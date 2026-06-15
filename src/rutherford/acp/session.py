# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ACP session: a live connection to one agent, driven across one or many prompt turns.

:class:`ACPSession` is the reusable primitive. It spawns the agent as an ACP server, performs the
``initialize`` / ``new_session`` handshake, and then runs any number of ``session/prompt`` turns on the
*same* live session -- the foundation for long-running conversations (a debate keeps one session per voice
across rounds, sending only deltas, instead of re-spawning and re-sending the whole transcript each time).
Each turn reduces its own event journal into a normalized :class:`~rutherford.domain.models.DelegationResult`
and classifies any failure's re-execution safety. :func:`run_acp_turn` is the one-shot wrapper (open, one
turn, close) used by ``delegate`` / ``consensus``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import AsyncExitStack
from pathlib import Path

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    ResourceContentBlock,
    TextContentBlock,
)

from ..domain.enums import Effort, ReexecutionSafety
from ..domain.error_codes import ErrorCode
from ..domain.models import Cost, DelegationResult, ErrorInfo, Provenance, Target
from ..runtime.depth import child_env
from .client import RutherfordACPClient
from .descriptors import AgentDescriptor
from .effort import effort_overrides
from .journal import EventJournal, journal_event_from_message
from .launch import prepare_argv
from .permission import PermissionPolicy
from .teardown import count_descendants, reap, snapshot_descendants

#: How often the live observed-agent sampler walks the agent's process tree during a turn (N1, item 3). A
#: coarse cadence: the sampler exists to catch a peak fan-out, not to track every transient process, and a
#: tighter loop would add psutil overhead to every turn for no extra fidelity.
_OBSERVE_INTERVAL_S = 0.5

#: How Rutherford identifies itself to an agent at ``initialize``.
_CLIENT_INFO = Implementation(name="rutherford-acp", version="3.0.0")
#: Max bytes in a single line of an agent's JSON-RPC stdout. asyncio's StreamReader default (64 KiB) is too
#: small for real agents -- one ``session/update`` can carry a big model list (kilo on OpenRouter enumerates
#: hundreds of models), a large file read, or a long tool output, and a line over the limit raises
#: "Separator is found, but chunk is longer than limit" and drops the connection. 16 MiB is generous for any
#: legitimate message while still bounding memory against a runaway agent.
_STREAM_LIMIT = 16 * 1024 * 1024
#: The ACP prompt content-block union (annotated so the single-text-block list types cleanly).
PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


class ACPHandshakeError(Exception):
    """A session could not be opened (spawn or handshake failed). Pre-prompt, so re-execution-safe.

    Carries the ACP error code and the re-execution-safety classification so a caller can turn it into a
    failed result or decide a fallback. Raised by :meth:`ACPSession.open`; :func:`run_acp_turn` converts it
    to a failed :class:`DelegationResult`.
    """

    def __init__(self, code: ErrorCode, message: str, safety: ReexecutionSafety) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.safety = safety


class ACPSession:
    """A live ACP conversation with one agent: open once, run many prompt turns, close.

    Not safe for concurrent turns on one instance -- one turn at a time (a conversation is sequential). The
    journal is swapped per turn, and a synchronous stream observer records each turn's ``session/update``
    stream inline in receive order, so a turn's journal is complete the moment its prompt response resolves.
    """

    def __init__(
        self,
        descriptor: AgentDescriptor,
        *,
        policy: PermissionPolicy,
        cwd: str,
        model: str | None = None,
        effort: Effort | None = None,
        base_depth: int = 0,
        parent_run_id: str | None = None,
        sandbox_root: str | None = None,
        resume_session_id: str | None = None,
        handshake_timeout_s: float | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._policy = policy
        # The per-step budget for each handshake call (initialize / new_session / load_session / set_model):
        # the descriptor's default, unless a caller overrides it. A handshake-only connection probe passes its
        # own budget here so a generous local-model floor actually reaches the handshake (descriptor's fixed
        # default would otherwise dominate, falsely failing a slow cold-model handshake).
        self._handshake_timeout = (
            handshake_timeout_s if handshake_timeout_s is not None else descriptor.handshake_timeout_s
        )
        # Resume a prior agent session over ACP (``session/load``) instead of creating a fresh one
        # (``session/new``): the opaque id from an earlier turn's result, round-tripped back so the agent
        # reloads that conversation. ``None`` is the default fresh-session path. Gated at open() on the agent
        # advertising the ``loadSession`` capability; a resume against an agent that cannot reload its own
        # sessions is a clean ``RESUME_FAILED`` rather than a silent fresh session.
        self._resume_session_id = resume_session_id
        # N1 (item 3): how deep this run sits in a Rutherford-driving-Rutherford chain, and the panel parent
        # to correlate a voice back to. Layered onto the agent's environment at open() so a nested host reads
        # them back (the recursion guard) and an aggregate cap can reason across layers (count-first lineage).
        self._base_depth = base_depth
        self._parent_run_id = parent_run_id
        #: The peak local descendant count psutil observed while a turn was live (N1, item 3): the agent
        #: process plus its sub-processes, a FLOOR (remote agents invisible). ``None`` until a turn samples it.
        self._observed_peak_agents: int | None = None
        # ACP requires an absolute cwd in session/new (a relative one, e.g. ".", is rejected by agents like
        # goose). Resolve once here so every path -- delegate, consensus, debate, the conformance probe --
        # hands the agent an absolute working directory.
        self._cwd = str(Path(cwd).resolve())
        # Resolve effort to this agent's per-call ACP override (extra args / env / a rewritten model id), or a
        # reported no-op when the agent has no knob (F8a, 2-L). The override is computed against the RESOLVED
        # model so codex/cursor (which encode effort in the model id) rewrite the model the session will use.
        resolved_model = model or descriptor.default_model
        self._effort = effort
        self._override = effort_overrides(descriptor, effort, model=resolved_model)
        self._target = Target(cli=descriptor.id, model=self._override.model or resolved_model)
        # F2 replay-completeness: the LOGICAL launch argv (the agent's ACP-server command plus any
        # effort-override extra args), pinned here so a persisted run records what it was issued with. Kept
        # distinct from the platform-resolved spawn argv (``prepare_argv`` below), whose npm-shim resolution
        # bakes in machine-specific absolute paths a replay on another host could not reuse.
        self._launch_argv = [*descriptor.command, *self._override.extra_args]
        # The FileGateway / TerminalBroker confinement root for a mutating sandbox (the worktree / temp copy);
        # None for a non-sandboxed session, where reads are served anywhere and terminal stays denied. Resolved
        # so the client's path-escape guard compares against a canonical absolute root.
        self._sandbox_root = str(Path(sandbox_root).resolve()) if sandbox_root is not None else None
        self._journal = EventJournal()
        self._client = RutherfordACPClient(
            journal=self._journal, policy=policy, cwd=self._cwd, sandbox_root=self._sandbox_root
        )
        self._stack = AsyncExitStack()
        self._conn: ClientSideConnection | None = None
        self._session_id: str | None = None
        self._pid: int | None = None
        #: The model ids the agent advertised at session open (``session.models``), captured so a caller can
        #: see what the agent offered -- the "configure" signal of a handshake-only connection check. ``[]``
        #: when the agent advertises no selectable models (it runs on its own default).
        self._available_models: list[str] = []

    @property
    def effort_applied(self) -> Effort | None:
        """The effort tier this session actually applied (clamped), or ``None`` for a no-op (F8a, 2-L)."""
        return self._override.applied

    @property
    def observed_peak_agents(self) -> int | None:
        """The peak local descendant count sampled while a turn ran (N1, item 3); a floor, ``None`` if unsampled."""
        return self._observed_peak_agents

    @property
    def target(self) -> Target:
        """The resolved ``(cli, model)`` this session answers under."""
        return self._target

    @property
    def launch_argv(self) -> list[str]:
        """The logical launch argv this session was issued with (F2 replay-completeness; see __init__)."""
        return list(self._launch_argv)

    @property
    def session_id(self) -> str | None:
        """The agent's session id once opened, for provenance and a later resume; ``None`` before open."""
        return self._session_id

    @property
    def available_models(self) -> list[str]:
        """The model ids the agent advertised at open (its selectable models); ``[]`` before open or if none."""
        return list(self._available_models)

    @property
    def partial_text(self) -> str:
        """The answer text streamed so far on the CURRENT turn, for a time-budget harvest of a cut voice.

        Read after a voice is cut at a panel's deadline: the turn never resolved, so its journal holds only
        what the agent streamed before the cut. Empty when nothing was streamed (a single-shot agent that
        emits its answer only at the end yields no partial, which the harvest records honestly).
        """
        return self._journal.message_text()

    async def __aenter__(self) -> ACPSession:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Spawn the agent and complete the handshake, or raise :class:`ACPHandshakeError`."""
        # Layer this turn's effort override onto the launch: extra env on top of the resolved environment, and
        # extra args appended to the agent's own argv (e.g. cline's ``--thinking high``). A model-id-encoding
        # agent (codex/cursor) carries its effort in ``self._target.model`` instead, applied via set_model below.
        # N1 (item 3): the depth + count-first lineage env goes on last, so a spawned agent that is itself a
        # Rutherford host reads where it sits (the recursion guard) and the aggregate cap counts across layers.
        env = {
            **_resolve_env(self._descriptor),
            **self._override.env_dict,
            **child_env(self._base_depth, parent_run_id=self._parent_run_id),
        }
        command, *args = prepare_argv(tuple(self._launch_argv))

        def _observe(event: StreamEvent) -> None:
            # SYNCHRONOUS observer: inline in receive order, so each turn's journal is complete before its
            # prompt response resolves. ``self._journal`` is swapped per turn, so this always writes the
            # current turn's journal.
            if event.direction is StreamDirection.INCOMING:
                entry = journal_event_from_message(event.message)
                if entry is not None:
                    self._journal.append(entry)

        try:
            conn, process = await self._stack.enter_async_context(
                spawn_agent_process(
                    self._client,
                    command,
                    *args,
                    env=env,
                    cwd=self._cwd,
                    transport_kwargs={"stderr": None, "limit": _STREAM_LIMIT},
                    observers=[_observe],
                )
            )
        except OSError as exc:
            # OSError, not just FileNotFoundError: a missing binary is FileNotFoundError, but a working_dir
            # that resolves to a file (NotADirectoryError) or an unexecutable command (PermissionError) is
            # also a launch failure, not an internal error. All map to a clean re-execution-safe spawn fail.
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.ACP_SPAWN_FAILED,
                f"could not launch {self._descriptor.id} ({command!r}): {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._conn = conn
        self._pid = process.pid
        # A cancellation ANYWHERE in the handshake (initialize / new_session / load / set_model) is a
        # BaseException, so the per-stage ``except Exception`` guards below do NOT catch it. Without this outer
        # guard the just-spawned agent would be left registered on the exit stack but never torn down -- a
        # leaked process tree, since ``run_acp_turn`` enters the session with ``async with`` and Python skips
        # ``__aexit__`` when ``__aenter__`` (this ``open``) raises. Tear the agent down on a cancel, then
        # re-raise so the cancellation still propagates (the per-stage handlers already close on an Exception).
        try:
            try:
                init = await asyncio.wait_for(
                    conn.initialize(protocol_version=PROTOCOL_VERSION, client_info=_CLIENT_INFO),
                    timeout=self._handshake_timeout,
                )
            except Exception as exc:
                await self.close()
                raise ACPHandshakeError(
                    ErrorCode.ACP_HANDSHAKE_FAILED,
                    f"ACP handshake with {self._descriptor.id} failed: {exc}",
                    ReexecutionSafety.SAFE,
                ) from exc
            # Resume a prior session (session/load) when asked, else create a fresh one (session/new). The
            # resume path is gated on the agent's advertised loadSession capability and fails RESUME_FAILED if
            # unsupported.
            session: NewSessionResponse | LoadSessionResponse
            if self._resume_session_id is not None:
                session = await self._resume(conn, init)
            else:
                session = await self._new_session(conn)
            self._available_models = _models_of(session)
            await self._select_model(conn, session)
        except asyncio.CancelledError:
            await self.close()
            raise

    async def _new_session(self, conn: ClientSideConnection) -> NewSessionResponse:
        """Create a fresh session (``session/new``); a failure is an ``ACP_HANDSHAKE_FAILED`` handshake fault."""
        try:
            session = await asyncio.wait_for(
                conn.new_session(cwd=self._cwd, mcp_servers=[]),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.ACP_HANDSHAKE_FAILED,
                f"ACP handshake with {self._descriptor.id} failed: {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._session_id = session.session_id
        return session

    async def _resume(self, conn: ClientSideConnection, init: InitializeResponse) -> LoadSessionResponse:
        """Resume the prior session via ACP ``session/load`` instead of ``session/new``.

        Gated on the agent advertising the ``loadSession`` capability at initialize: an agent that does not
        persist and reload its own sessions cannot resume, so a resume against it is a clean ``RESUME_FAILED``
        rather than a silent fresh session. ``session/load`` does not mint a new id -- the loaded session keeps
        the requested one. SAFE re-execution: the resume is pre-prompt, with no side effect or cost.
        """
        resume_id = self._resume_session_id
        assert resume_id is not None  # guarded by the caller (only taken when a resume id is set)
        caps = init.agent_capabilities
        if caps is None or not caps.load_session:
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.RESUME_FAILED,
                f"{self._descriptor.id} cannot resume a session: it does not advertise the ACP loadSession "
                "capability (it does not persist sessions for reload)",
                ReexecutionSafety.SAFE,
            )
        try:
            session = await asyncio.wait_for(
                conn.load_session(cwd=self._cwd, session_id=resume_id, mcp_servers=[]),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.RESUME_FAILED,
                f"resuming session {resume_id!r} on {self._descriptor.id} failed: {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._session_id = resume_id
        return session

    async def _select_model(
        self, conn: ClientSideConnection, session: NewSessionResponse | LoadSessionResponse
    ) -> None:
        """Best-effort ``session/set_model`` to the resolved model, so a chosen model (and a model-id-encoded
        effort tier for codex/cursor) actually takes effect over ACP. Never fatal.

        The model is sent only when one is resolved AND the agent advertised it among ``session.models`` from
        ``new_session`` -- so an agent that takes no model (or does not offer this one) is left on its default
        rather than handed an unknown id. Any failure is swallowed: model selection is an enhancement, not a
        handshake requirement, and the turn proceeds on the agent's default model.
        """
        model = self._target.model
        if not model or self._session_id is None:
            return
        if not _advertises_model(session, model):
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                conn.set_session_model(model_id=model, session_id=self._session_id),
                timeout=self._handshake_timeout,
            )

    async def prompt(self, text: str, *, timeout_s: float) -> DelegationResult:
        """Run one prompt turn on the live session and return its normalized result.

        Never raises for an operational failure (timeout / refusal / empty / transport error): each becomes
        a failed :class:`DelegationResult` with an ACP error code. ``open`` must have succeeded first.
        """
        if self._conn is None or self._session_id is None:  # pragma: no cover - guarded by open()
            raise RuntimeError("ACPSession.prompt called before a successful open()")
        self._journal = EventJournal()
        self._client.journal = self._journal
        start = time.monotonic()
        blocks: list[PromptBlock] = [text_block(text)]
        # N1 (item 3): sample the agent's local process tree on a coarse timer for the duration of the turn,
        # keeping the peak descendant count -- a FLOOR for how many agents this voice spun up. Started here
        # and always stopped in the finally, so a timeout/error path still records what it saw before the cut.
        sampler = asyncio.create_task(self._sample_observed_agents())
        try:
            response = await asyncio.wait_for(
                self._conn.prompt(prompt=blocks, session_id=self._session_id),
                timeout=timeout_s,
            )
        except TimeoutError:
            await self.cancel()
            return self._stamp(
                _failed(
                    self._target,
                    self._policy,
                    start,
                    ErrorCode.ACP_TURN_TIMEOUT,
                    f"{self._descriptor.id} did not finish within {timeout_s:.0f}s",
                    _post_prompt_safety(self._journal),
                    partial=self._journal.message_text() or None,
                )
            )
        except Exception as exc:
            return self._stamp(
                _failed(
                    self._target,
                    self._policy,
                    start,
                    ErrorCode.ACP_TURN_ERROR,
                    f"ACP turn for {self._descriptor.id} failed: {exc}",
                    ReexecutionSafety.AMBIGUOUS,
                )
            )
        finally:
            # Stop the sampler and fold its final reading in, so even a timeout/error path records the peak it
            # saw. Cancel-then-await keeps no sampler task dangling on the loop. Best-effort: never raises.
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
        result = _reduce(self._descriptor, self._target, self._policy, self._journal, response, self._session_id, start)
        return self._stamp(result)

    async def _sample_observed_agents(self) -> None:
        """Poll the agent's process tree on a coarse timer, keeping the peak descendant count (N1, item 3).

        Runs off-thread (psutil is blocking) for the life of a turn; cancelled in :meth:`prompt`'s finally.
        Each sample is the agent pid plus its recursive children -- a FLOOR, since a sample can lose the race
        with a transient sub-process and psutil sees only local processes. A ``0`` sample (the pid already
        gone) never lowers the peak. Best-effort: the loop swallows everything but a cancellation.
        """
        pid = self._pid
        if pid is None:  # pragma: no cover - prompt() is guarded by a successful open() that set the pid
            return
        try:
            while True:
                count = await asyncio.to_thread(count_descendants, pid)
                if count > 0 and (self._observed_peak_agents is None or count > self._observed_peak_agents):
                    self._observed_peak_agents = count
                await asyncio.sleep(_OBSERVE_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a transparency sampler must never break the turn it observes
            return

    def _stamp(self, result: DelegationResult) -> DelegationResult:
        """Stamp the per-turn metadata onto the result: the effort tiers (F8a) and the observed peak (N1).

        ``effort`` / ``effort_applied`` echo what was requested and what the agent applied after clamping;
        ``observed_peak_agents`` carries the live sampler's high-water mark up so a panel can roll it into
        its :class:`~rutherford.domain.models.Topology` (a floor, ``None`` when nothing was sampled); ``argv``
        pins the logical launch for an F2 replay.
        """
        result.effort = self._effort
        result.effort_applied = self._override.applied
        result.observed_peak_agents = self._observed_peak_agents
        result.argv = list(self._launch_argv)
        return result

    async def cancel(self) -> None:
        """Best-effort ``session/cancel`` for an in-flight turn; never raises."""
        if self._conn is not None and self._session_id is not None:
            with contextlib.suppress(Exception):
                await self._conn.cancel(session_id=self._session_id)

    async def close(self) -> None:
        """Tear down the connection and reap the agent's orphaned descendant processes. Idempotent.

        The SDK transport terminates only the direct child (the adapter). The descendants it spawns -- the
        underlying CLI a wrapper adapter fronts -- are snapshotted here *before* that termination (a dead
        parent's children reparent and drop out of the walk) and reaped after, so no orphaned CLI process is
        left holding the working directory. Best-effort: a teardown failure never propagates.
        """
        pid, self._pid = self._pid, None
        descendants = await asyncio.to_thread(snapshot_descendants, pid) if pid is not None else []
        # Tear down any brokered terminal the agent left running BEFORE the transport closes, so a write-mode
        # command (a build/test the agent kicked off) is killed and reaped rather than orphaned in the sandbox.
        with contextlib.suppress(Exception):
            await self._client.shutdown_terminals()
        try:
            # Suppressed so the documented "a teardown failure never propagates" contract actually holds: a
            # transport teardown error (e.g. a half-open async generator) must not propagate -- it would mask a
            # cancellation in flight when open()'s cancel handler calls close() before re-raising, and corrupt
            # the exception an ``async with session`` body was already unwinding. The reap still runs regardless.
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        finally:
            if descendants:
                await asyncio.to_thread(reap, descendants)
            self._conn = None


async def run_acp_turn(
    descriptor: AgentDescriptor,
    prompt: str,
    *,
    policy: PermissionPolicy,
    cwd: str,
    timeout_s: float,
    model: str | None = None,
    effort: Effort | None = None,
    base_depth: int = 0,
    parent_run_id: str | None = None,
    sandbox_root: str | None = None,
    resume_session_id: str | None = None,
) -> DelegationResult:
    """Open a one-shot session, run a single prompt turn, and return the normalized result.

    The spawn-per-delegation path for ``delegate`` / ``consensus``. ``effort`` is the reasoning-effort tier to
    apply over ACP (per-agent env / args / a model-id rewrite); it is echoed on the result as ``effort`` and
    ``effort_applied`` (F8a, 2-L). ``base_depth`` / ``parent_run_id`` are the N1 lineage signal layered onto
    the agent's environment so a Rutherford-driving-Rutherford chain is bounded. ``sandbox_root`` confines the
    agent's file/terminal callbacks to an isolated worktree / copy for a mutating run. ``resume_session_id``
    resumes a prior agent session (ACP ``session/load``) instead of opening a fresh one, where the agent
    supports it -- otherwise the turn fails ``RESUME_FAILED``. Never raises for an operational failure; a
    handshake / spawn / resume failure becomes a failed :class:`DelegationResult` (re-execution-safe), still
    carrying the requested effort.
    """
    start = time.monotonic()
    session = ACPSession(
        descriptor,
        policy=policy,
        cwd=cwd,
        model=model,
        effort=effort,
        base_depth=base_depth,
        parent_run_id=parent_run_id,
        sandbox_root=sandbox_root,
        resume_session_id=resume_session_id,
    )
    try:
        async with session:
            return await session.prompt(prompt, timeout_s=timeout_s)
    except ACPHandshakeError as exc:
        result = _failed(session.target, policy, start, exc.code, exc.message, exc.safety)
        result.effort = effort
        result.effort_applied = session.effort_applied
        result.argv = session.launch_argv  # F2: a spawn-failed leaf still records the argv it tried
        return result


def _reduce(
    descriptor: AgentDescriptor,
    target: Target,
    policy: PermissionPolicy,
    journal: EventJournal,
    response: PromptResponse,
    session_id: str,
    start: float,
) -> DelegationResult:
    """Project the finished turn's journal + stop reason into a normalized result."""
    text = journal.message_text().strip()
    cost = journal.usage()
    if response.stop_reason == "refusal":
        return _failed(
            target,
            policy,
            start,
            ErrorCode.ACP_REFUSED,
            f"{descriptor.id} refused the request",
            ReexecutionSafety.DUPLICATE_COST,
            cost=cost,
        )
    if not text:
        return _failed(
            target,
            policy,
            start,
            ErrorCode.ACP_EMPTY_ANSWER,
            f"{descriptor.id} ended the turn ({response.stop_reason}) with no answer text",
            ReexecutionSafety.DUPLICATE_COST,
            cost=cost,
        )
    return DelegationResult(
        target=target,
        ok=True,
        text=text,
        cost=cost,
        session_id=session_id,
        duration_s=round(time.monotonic() - start, 3),
        provenance=Provenance(provider=descriptor.provider, model=target.model, confirmed=False),
        safety_mode=policy.mode,
    )


def _failed(
    target: Target,
    policy: PermissionPolicy,
    start: float,
    code: ErrorCode,
    message: str,
    safety: ReexecutionSafety,
    *,
    partial: str | None = None,
    cost: Cost | None = None,
) -> DelegationResult:
    """Build a failed result carrying the ACP error code and its re-execution-safety classification."""
    return DelegationResult(
        target=target,
        ok=False,
        duration_s=round(time.monotonic() - start, 3),
        error=ErrorInfo(code=code, message=message, reexecution_safety=safety),
        partial=partial,
        cost=cost,
        safety_mode=policy.mode,
    )


def _post_prompt_safety(journal: EventJournal) -> ReexecutionSafety:
    """Classify how unsafe a re-run is after the prompt was accepted, from what the journal observed."""
    if journal.saw_side_effect():
        return ReexecutionSafety.SIDE_EFFECTED
    if journal.saw_tool_activity():
        return ReexecutionSafety.AMBIGUOUS
    return ReexecutionSafety.DUPLICATE_COST


def _resolve_env(descriptor: AgentDescriptor) -> dict[str, str]:
    """The environment for the agent subprocess: inherited (or allowlisted), then config overrides on top."""
    if descriptor.env_passthrough is None:
        env = dict(os.environ)
    else:
        env = {name: os.environ[name] for name in descriptor.env_passthrough if name in os.environ}
    env.update(descriptor.env_overrides)
    return env


def _advertises_model(session: NewSessionResponse | LoadSessionResponse, model_id: str) -> bool:
    """Whether the session advertised ``model_id`` among its selectable models (so set_model is safe)."""
    state = session.models
    if state is None or state.available_models is None:
        return False
    return any(info.model_id == model_id for info in state.available_models)


def _models_of(session: NewSessionResponse | LoadSessionResponse) -> list[str]:
    """The model ids the session advertised (its selectable models), or ``[]`` when it offers none."""
    state = session.models
    if state is None or state.available_models is None:
        return []
    return [info.model_id for info in state.available_models]
