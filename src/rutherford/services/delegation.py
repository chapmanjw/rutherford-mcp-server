# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The delegation service: hand one request to one ACP agent and return the normalized envelope.

The ACP-native foundational primitive every tool bottoms out in. It resolves the agent descriptor, builds
the :class:`~rutherford.acp.permission.PermissionPolicy` from the safety mode (guarding the mutating modes
behind a trusted-workspace check), enforces the cross-cutting guards (the recursion-depth cap and the global
concurrency semaphore), composes the prompt with any in-scope files, and drives one ACP turn via
:func:`~rutherford.acp.session.run_acp_turn`. Every operational failure is returned as a structured
:class:`DelegationResult`, never raised, so a consensus panel never aborts on one bad voice.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..acp.cooldown import CooldownTracker
from ..acp.descriptors import AgentDescriptor, DescriptorRegistry
from ..acp.failures import indicates_unhealthy, is_model_unavailable
from ..acp.permission import PermissionPolicy
from ..acp.sandbox import SandboxManager
from ..acp.session import run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import ActivityEventKind, Effort, JobStatus, ReexecutionSafety, is_mutating, runs_sandboxed
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ActivityEvent, DelegationRequest, DelegationResult, ErrorInfo, RunRecord, Target, Topology
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_depth
from ..runtime.logging import log_event

#: How long the ``verify_read_only`` git fingerprint may take before it is abandoned (its check skipped). A
#: fingerprint is two cheap git reads; the bound exists only so a wedged git can never stall a delegation.
_FINGERPRINT_TIMEOUT_S = 30.0

#: The structured live-activity sink (N1, item 3): lifecycle :class:`ActivityEvent`s a service emits as a run
#: progresses (a voice starting/finishing, a panel boundary, a budget cut). A sync tool maps each to an MCP
#: progress push; a background job buffers them for the ``activity`` poll table. Best-effort: a raising sink
#: never breaks the run it only observes.
ActivityCallback = Callable[[ActivityEvent], None]


def emit_activity(on_activity: ActivityCallback | None, event: ActivityEvent) -> None:
    """Deliver an :class:`ActivityEvent` to a sink if one is listening; swallow any error (N1, item 3).

    Transparency is a side-channel: a buggy or slow activity sink must never fail (or abort) the run it is
    only observing, so every emission goes through here and a raising sink is silently dropped.
    """
    if on_activity is None:
        return
    with contextlib.suppress(Exception):  # a transparency sink must never break the run it observes
        on_activity(event)


def panel_cancelled_event(tool: str, depth: int) -> ActivityEvent:
    """The terminal ``job_cancelled`` event for a panel cancelled after it started (N1, item 3, 3-K).

    Emitted (via :class:`PanelLifecycle`) when a panel is cancelled after ``panel_started`` so the activity
    stream always closes with one terminal event rather than being orphaned -- a cancel can land at any of the
    panel's awaits, so the guarantee is centralized rather than guarded await-by-await.
    """
    return ActivityEvent(
        kind=ActivityEventKind.JOB_CANCELLED, tool=tool, depth=depth, status="cut", message=f"{tool} panel cancelled"
    )


class PanelLifecycle:
    """Guarantees a panel's activity stream emits EXACTLY ONE terminal event (N1, item 3, decision 3-K).

    A panel emits ``panel_started`` once it is past its up-front guards, then -- after any number of awaits
    (voice waits, the closing synthesis, the persist) -- exactly one terminal: ``panel_finished`` on a clean
    finish or a budget-exhausted failure, or ``job_cancelled`` if it is cancelled anywhere in between. Because
    a cancellation can surface at any of those awaits, the panel body is wrapped once (see the panel services)
    and the terminal is emitted here -- tracking ``started`` (so a cancel BEFORE the panel started emits
    nothing) and ``closed`` (so a terminal is never emitted twice).
    """

    def __init__(self, tool: str, depth: int, on_activity: ActivityCallback | None) -> None:
        self._tool = tool
        self._depth = depth
        self._on_activity = on_activity
        self._started = False
        self._closed = False

    def mark_started(self, event: ActivityEvent) -> None:
        """Emit the ``panel_started`` event and record that the panel is live."""
        self._started = True
        emit_activity(self._on_activity, event)

    def mark_closed(self, event: ActivityEvent) -> None:
        """Emit a terminal ``panel_finished`` event (clean or failed) and record the panel as closed."""
        self._closed = True
        emit_activity(self._on_activity, event)

    def on_cancel(self) -> None:
        """Emit the terminal ``job_cancelled`` -- but only if the panel started and has not already closed."""
        if self._started and not self._closed:
            self._closed = True
            emit_activity(self._on_activity, panel_cancelled_event(self._tool, self._depth))


class DelegationService:
    """Executes a single ACP delegation end to end."""

    def __init__(
        self,
        descriptors: DescriptorRegistry,
        config: RutherfordConfig,
        *,
        cooldown: CooldownTracker | None = None,
        ledger: RunLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._descriptors = descriptors
        self._config = config
        #: The durable run ledger (F2). ``None`` disables persistence entirely (e.g. a test with no jobs dir
        #: configured); when set, a run opting into persistence is written under its root as a leaf record.
        self._ledger = ledger
        #: Wall-clock source for run-record timestamps, injectable so persistence is testable.
        self._clock = clock
        #: Bounds how many ACP agent sessions run at once across every panel that shares this service
        #: (the consensus fan-out, a debate's rounds, a nested self-delegation), so panel width does not
        #: become unbounded host process pressure (N1 / reliability). Held only around the ACP turn.
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        #: The per-agent cooldown tracker (F7): each delegation records its turn's health here (a success
        #: clears the streak, an UNHEALTHY ACP failure counts toward a bench), and the cross-target fallback
        #: chain skips a benched alternate. Injected so the delegation primitive and the consensus
        #: auto-panel read the SAME bench state; defaults to a tracker built from config when not supplied
        #: (so a directly-constructed service in a test still honours the configured thresholds).
        self._cooldown = cooldown or CooldownTracker(
            threshold=config.cooldown_threshold,
            window_s=config.cooldown_window_s,
            duration_s=config.cooldown_duration_s,
        )
        #: Builds the isolated execution root (git worktree, or temp copy for a non-git tree) a mutating
        #: delegation runs in, so an agent's write/yolo/propose edits land in a throwaway tree and only a
        #: reviewed diff is applied back. Stateless across calls.
        self._sandbox = SandboxManager()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """The shared concurrency semaphore, so a panel's budgeted/direct-session paths gate the SAME way.

        The consensus budget harvest and a debate round drive :class:`~rutherford.acp.session.ACPSession`
        turns directly (not through :meth:`delegate`), so they acquire this around each turn to keep one
        global ``max_concurrency`` ceiling across every path that spawns an agent.
        """
        return self._semaphore

    def is_benched(self, agent_id: str) -> bool:
        """Whether ``agent_id`` is currently on cooldown (F7), so the consensus auto-panel can skip it."""
        return self._cooldown.is_benched(agent_id)

    def cooldown_remaining_s(self, agent_id: str) -> float:
        """Seconds until ``agent_id``'s cooldown bench lifts (``0.0`` when not benched), for the skip reason."""
        return self._cooldown.remaining_s(agent_id)

    async def delegate(
        self,
        req: DelegationRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_activity: ActivityCallback | None = None,
    ) -> DelegationResult:
        """Run ``req`` against its target agent (with fallback) and return the normalized result.

        ``base_depth`` (N1, item 3) is how deep this delegation sits in a Rutherford-driving-Rutherford chain:
        it is checked against ``max_depth`` (refused with ``MAX_DEPTH_EXCEEDED`` at the ceiling) and layered
        onto the spawned agent's environment so a nested host stays bounded. ``on_activity`` receives the
        ``voice_started`` (once the concurrency slot is acquired and the turn launches) and ``voice_finished``
        events so a sync caller is pushed live progress and a job buffers the per-voice table.

        Fallback (F7), only ever on a SAFE failure (``error.reexecution_safety is SAFE`` -- a pre-prompt spawn
        or handshake failure that could not have spent cost or caused a side effect; a DUPLICATE_COST /
        AMBIGUOUS / SIDE_EFFECTED failure is NEVER retried) and only on a non-mutating delegation (a write/yolo
        run may have partially mutated the tree, so it is never re-run elsewhere): first a same-agent retry on
        the agent's configured ``fallback_model`` when the failure looks model-unavailable
        (``allow_model_fallback``), then each ``req.fallback`` alternate in turn (a benched alternate skipped),
        until one answers. ``fallback_from`` records the requested model when a model fallback fired;
        ``fallback_chain`` records the labels of the targets that failed before the one that answered; and
        ``delegation_call_count`` counts every subprocess attempt (the primary plus each fallback re-run), so a
        panel's realized fan-out includes the fallbacks.
        """
        created_at = self._clock()
        if not self._descriptors.has(req.target.cli):
            known = ", ".join(self._descriptors.ids()) or "(none)"
            return _fail(req, ErrorCode.UNKNOWN_TARGET, f"unknown agent id {req.target.cli!r}; known agents: {known}")

        try:
            ensure_within_depth(base_depth, self._config.max_depth)
        except RutherfordError as exc:
            return _fail(req, exc.code, exc.message, details=exc.details)

        # A sandboxed mode (propose / write / yolo) MUST have a working_dir: it is the tree the sandbox is
        # built from. Without one there is nothing to isolate and the turn would fall through to the direct
        # path in the server's own cwd with writes allowed -- an unsandboxed write into Rutherford's directory.
        # So require it up front (this also closes a trust_workspace=true + no-working_dir bypass).
        if runs_sandboxed(req.safety_mode) and not req.working_dir:
            return _fail(
                req,
                ErrorCode.INVALID_INPUT,
                f"{req.safety_mode.value} mode needs a working_dir to sandbox into: without one there is no "
                "tree to isolate. Pass the absolute path of the workspace the agent should operate on.",
            )
        if is_mutating(req.safety_mode) and not self._workspace_trusted(req):
            return _fail(
                req,
                ErrorCode.WORKSPACE_NOT_TRUSTED,
                f"{req.safety_mode.value} mode requires a trusted workspace; set trust_workspace=true "
                "or add the directory to trusted_workspaces in config",
            )

        # An EXPLICIT delegation to a benched agent STILL RUNS: cooldown shapes auto-selection (the
        # expand_all panel, a fallback candidate), it never blocks a direct request the caller chose on
        # purpose. So there is no bench check on the primary here.
        self._emit_started(on_activity, req, correlation_id, base_depth)
        result = await self._run_turn(req, base_depth)
        attempts = 1

        # Same-agent model fallback (F7): a model-unavailable SAFE failure retries the SAME agent on its
        # configured fallback_model, where it has one. Most ACP agents do not, so this is a clean no-op.
        fb_model = self._model_fallback_for(req, result)
        if fb_model is not None:
            result = await self._model_fallback(req, base_depth, fb_model)
            attempts += 1

        # Cross-target fallback (F7): a SAFE, non-mutating failure with a fallback chain tries each alternate
        # in turn. A benched alternate is skipped. A winning alternate's result is adopted whole (its own
        # provenance / health / count); the chain total is folded into the returned count either way.
        if self._should_cross_fallback(req, result):
            recovered, alternate_attempts = await self._fallback_chain(req, result, correlation_id, base_depth)
            attempts += alternate_attempts
            if recovered is not None:
                # The winning alternate persisted its OWN leaf record inside its recursive ``delegate`` call
                # (it ran through this same path), and the failed primary is not persisted -- matching v2: a
                # cross-target fallback's leaf ``state.json`` records the run that answered, not the whole chain.
                recovered.delegation_call_count = attempts
                emit_activity(on_activity, _voice_finished_event(recovered, req.role, base_depth, correlation_id))
                return recovered

        # N1 (decision 3-A): the subprocess delegations this seat launched -- the primary plus a model
        # fallback re-run plus every cross-target alternate tried (win or lose) -- so a panel's realized
        # fan-out counts the fallback re-runs.
        result.delegation_call_count = attempts
        # F2: persist this run as a durable leaf job when the call opts in -- best-effort, off-thread (file
        # I/O), and never failing the run that already produced an answer. A no-op for an ephemeral run.
        if self._ledger is not None and self._should_persist(req):
            await asyncio.to_thread(self._maybe_persist, req, result, created_at)
        emit_activity(on_activity, _voice_finished_event(result, req.role, base_depth, correlation_id))
        return result

    async def _run_turn(self, req: DelegationRequest, base_depth: int) -> DelegationResult:
        """Run one ACP turn for ``req`` (gated on the semaphore) and feed the cooldown tracker its health.

        The single-turn primitive the primary and every fallback re-run bottom out in. A mutating mode
        (``propose`` / ``write`` / ``yolo``) with a working directory runs inside an isolated SANDBOX -- the
        agent never touches the user's tree; ``propose`` discards the worktree and returns the diff, ``write``
        / ``yolo`` apply the diff back. A ``read_only`` (or un-sandboxable) run executes directly in ``cwd``,
        optionally fingerprinted by ``verify_read_only``. Recording health here means each agent the chain
        touches counts its OWN turn toward (or clears) its OWN bench.
        """
        descriptor = self._descriptors.get(req.target.cli)
        cwd = req.working_dir or str(Path.cwd())
        timeout = req.timeout_s or self._config.timeout_for(req.target.cli) or self._config.default_timeout_s
        prompt = _compose_prompt(req.prompt, req.files)
        if runs_sandboxed(req.safety_mode) and req.working_dir:
            result = await self._run_sandboxed(req, descriptor, prompt, cwd, timeout_s=timeout, base_depth=base_depth)
        else:
            result = await self._run_direct(req, descriptor, prompt, cwd, timeout_s=timeout, base_depth=base_depth)
        result.delegation_call_count = 1
        self._record_health(req.target.cli, result)
        return result

    async def _run_direct(
        self,
        req: DelegationRequest,
        descriptor: AgentDescriptor,
        prompt: str,
        cwd: str,
        *,
        timeout_s: float,
        base_depth: int,
    ) -> DelegationResult:
        """Run the turn directly in ``cwd`` (no sandbox), with the optional ``verify_read_only`` fingerprint.

        The path for ``read_only`` (and any mutating mode with no ``working_dir`` to isolate, where the policy
        already denies writes). When ``verify_read_only`` is on and ``cwd`` is a git repo, the tree under it is
        fingerprinted before and after a SUCCESSFUL turn; a change fails the result with ``READONLY_VIOLATED``
        -- the agent's read-only promise made a checked invariant rather than a trusted one.
        """
        policy = PermissionPolicy(mode=req.safety_mode, sandboxed=False)
        verify = self._config.verify_read_only and not is_mutating(req.safety_mode)
        before = _git_fingerprint(cwd) if verify else None
        async with self._semaphore:
            result = await run_acp_turn(
                descriptor,
                prompt,
                policy=policy,
                cwd=cwd,
                timeout_s=timeout_s,
                model=req.target.model,
                effort=self.resolve_effort(req.target.cli, req.effort),
                base_depth=base_depth,
                parent_run_id=req.parent_run_id,
                resume_session_id=req.session_id,  # resume a prior agent session over ACP, where supported
            )
        # Check the fingerprint whether or not the turn SUCCEEDED: a read-only agent that mutated the tree and
        # then failed (or returned empty) still broke the read-only promise, and the side effect is the signal
        # that matters -- the caller should see READONLY_VIOLATED, not an ordinary failure that hides the write.
        if verify and before is not None:
            after = _git_fingerprint(cwd)
            if after is not None and after != before:
                return _readonly_violated(req, result)
        return result

    async def _run_sandboxed(
        self,
        req: DelegationRequest,
        descriptor: AgentDescriptor,
        prompt: str,
        cwd: str,
        *,
        timeout_s: float,
        base_depth: int,
    ) -> DelegationResult:
        """Run a mutating turn inside an isolated worktree / temp copy; capture (and for write/yolo apply) the diff.

        The agent's spawn cwd, ACP ``session/new`` cwd, and file/terminal confinement root are all the sandbox
        root, so its edits land in the throwaway tree, never the user's. After the turn the changed set is
        computed: ``propose`` discards it (the real tree is untouched, the diff is the deliverable); ``write`` /
        ``yolo`` apply it back to ``working_dir``. The sandbox is always cleaned up in the ``finally``, and the
        agent's process tree is reaped by the session teardown. A sandbox setup failure (e.g. a non-git tree
        over the copy guard) becomes a failed result rather than an unsandboxed run -- write mode never silently
        runs against the user's tree.
        """
        # Open under a shield so a cancellation mid-open cannot strand a half-built worktree / temp copy: the
        # open runs in a thread (which cannot be cancelled), so on a cancel we still await the shielded open to
        # recover the handle, clean it up, then propagate -- otherwise the dir (and a git worktree admin entry)
        # would leak because the ``finally`` below never sees a ``sandbox``.
        open_task = asyncio.ensure_future(asyncio.to_thread(self._sandbox.open, cwd))
        try:
            sandbox = await asyncio.shield(open_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                stranded = await open_task
                await asyncio.to_thread(stranded.cleanup)
            raise
        except RutherfordError as exc:
            return _fail(req, exc.code, exc.message, details=exc.details)
        except OSError as exc:
            # Building the sandbox is filesystem I/O (mkdtemp / copytree / mkdir): a failure (disk full, a
            # permission error, a vanished file mid-copy) is an operational fault, not a crash -- the delegation
            # primitive contract is that every fault returns a structured result, never raises onto the panel.
            return _fail(req, ErrorCode.INTERNAL, f"could not build the write sandbox for {cwd}: {exc}")
        policy = PermissionPolicy(mode=req.safety_mode, sandboxed=True)
        try:
            async with self._semaphore:
                result = await run_acp_turn(
                    descriptor,
                    prompt,
                    policy=policy,
                    cwd=sandbox.root,
                    timeout_s=timeout_s,
                    model=req.target.model,
                    effort=self.resolve_effort(req.target.cli, req.effort),
                    base_depth=base_depth,
                    parent_run_id=req.parent_run_id,
                    sandbox_root=sandbox.root,
                    resume_session_id=req.session_id,  # resume a prior agent session (conversation, not the tree)
                )
            if result.ok:
                try:
                    outcome = await asyncio.to_thread(sandbox.finish, req.safety_mode)
                except RutherfordError as exc:
                    return _fail(req, exc.code, f"sandbox apply failed: {exc.message}", details=exc.details)
                except OSError as exc:
                    # Computing the diff / applying it back is filesystem I/O; an OSError here (e.g. mkdir on an
                    # unwritable produce target, disk full) is a structured failure, never an uncaught raise.
                    return _fail(req, ErrorCode.INTERNAL, f"sandbox apply failed for {cwd}: {exc}")
                result.changed_files = outcome.changed_files
                result.diff = outcome.diff or None
                result.changes_applied = outcome.applied
        finally:
            await asyncio.to_thread(sandbox.cleanup)
        return result

    def _emit_started(
        self, on_activity: ActivityCallback | None, req: DelegationRequest, correlation_id: str, base_depth: int
    ) -> None:
        """Emit this voice's single ``voice_started`` under its stable correlation id (N1, item 3).

        Emitted once for the whole delegation -- a model/cross-target fallback re-run keeps the SAME
        correlation id (so the activity table collapses the voice to one row even as ``model`` changes
        mid-fallback) and does NOT emit a second ``started``.
        """
        emit_activity(
            on_activity,
            ActivityEvent(
                kind=ActivityEventKind.VOICE_STARTED,
                correlation_id=correlation_id,  # the stable per-voice key
                cli=req.target.cli,
                model=req.target.model,
                role=req.role,
                depth=base_depth,
                status="started",
                message=f"{req.target.display_label} started",
            ),
        )

    def resolve_effort(self, cli: str, effort: Effort | None) -> Effort | None:
        """The reasoning-effort tier a ``cli`` voice runs with (F8a, 2-L): the call value, else the config default.

        The single resolution rule -- call ``effort`` wins, else the per-agent ``[agents.<id>] effort``, else
        the global ``default_effort``, else ``None`` (let the agent decide). Shared by the delegation primitive
        and the panels (consensus/debate read it for each voice's rollup, including a voice cut at a deadline),
        so the precedence can never silently diverge across paths.
        """
        return effort if effort is not None else self._config.effort_for(cli)

    def _record_health(self, agent_id: str, result: DelegationResult) -> None:
        """Feed the cooldown tracker from a finished turn (F7): a success clears ``agent_id``'s failure
        streak; an UNHEALTHY failure (down / throttled / mis-launching / hung -- not a refusal, an empty
        answer, or a bad-prompt guard) counts toward benching it."""
        if result.ok:
            self._cooldown.record_success(agent_id)
        elif result.error is not None and indicates_unhealthy(result.error.code):
            self._cooldown.record_failure(agent_id)

    def _model_fallback_for(self, req: DelegationRequest, result: DelegationResult) -> str | None:
        """The agent's configured fallback model to retry once with, or ``None`` when no retry should happen.

        Only when the caller allowed it (``allow_model_fallback``), the run FAILED on a SAFE
        (re-execution-safe) model-availability error, and the agent declares a ``fallback_model`` that differs
        from what was already requested. A SAFE gate matters here too: a model-unavailable rejection that
        arrived only after the prompt was accepted (DUPLICATE_COST and up) must not be silently re-run.
        """
        if not req.allow_model_fallback or result.ok or result.error is None:
            return None
        if result.error.reexecution_safety is not ReexecutionSafety.SAFE:
            return None
        if not is_model_unavailable(result.error.message):
            return None
        fallback = self._descriptors.get(req.target.cli).fallback_model
        return fallback if fallback is not None and fallback != req.target.model else None

    async def _model_fallback(self, req: DelegationRequest, base_depth: int, fallback_model: str) -> DelegationResult:
        """Re-run the request once on the SAME agent with ``fallback_model``, recording the original model.

        The retry runs through :meth:`_run_turn` (so it counts toward the agent's health) with
        ``allow_model_fallback`` cleared so it cannot recurse. ``fallback_from`` records what was originally
        asked; ``result.target.model`` then holds the model that actually answered.
        """
        original_model = req.target.model
        fb_target = req.target.model_copy(update={"model": fallback_model})
        fb_req = req.model_copy(update={"target": fb_target, "allow_model_fallback": False})
        result = await self._run_turn(fb_req, base_depth)
        result.fallback_from = original_model or "(default)"
        return result

    def _should_cross_fallback(self, req: DelegationRequest, result: DelegationResult) -> bool:
        """Whether to try the cross-target fallback chain (F7): a SAFE, non-mutating failure with a chain.

        SAFE only -- a DUPLICATE_COST / AMBIGUOUS / SIDE_EFFECTED failure may have spent cost or mutated the
        tree, so it is never re-issued elsewhere. Non-mutating only -- retrying a write/yolo task on a second
        agent against the same (possibly partially-mutated) working_dir would compound two agents' edits, so
        write-mode reliability waits for worktree isolation.
        """
        return (
            not result.ok
            and bool(req.fallback)
            and not is_mutating(req.safety_mode)
            and result.error is not None
            and result.error.reexecution_safety is ReexecutionSafety.SAFE
        )

    async def _fallback_chain(
        self, req: DelegationRequest, primary: DelegationResult, correlation_id: str, base_depth: int
    ) -> tuple[DelegationResult | None, int]:
        """Try each fallback target in order; return the first successful result and the alternates' attempts.

        The failed primary leads the recorded ``fallback_chain`` (by its effective label -- the
        post-model-fallback target that actually failed). A BENCHED (cooled-down) alternate is skipped -- but
        recorded in the chain as ``<label> (benched)`` so the reader sees it was passed over, not absent. Each
        alternate is delegated fresh with no further fallback (so the chain cannot recurse), no carried
        ``session_id`` (a different agent's resume token does not transfer), and the SAME correlation id (so
        the voice stays one activity row), at the same depth (a sibling retry, not a deeper delegation). The
        chain is capped at ``max_targets`` so a long list cannot fan out unbounded. Returns
        ``(recovered_or_None, alternate_attempts)``: the first successful result (or ``None`` when every
        alternate failed, so the caller keeps the primary's failure) AND the total subprocess delegations every
        alternate tried, so the caller folds the whole chain into the realized fan-out count.
        """
        failed_labels = [primary.target.display_label]
        alternate_attempts = 0
        for target in req.fallback[: self._config.max_targets]:
            if self._cooldown.is_benched(target.cli):
                failed_labels.append(f"{target.display_label} (benched)")
                continue
            fb_req = req.model_copy(
                update={"target": target, "fallback": [], "allow_model_fallback": False, "session_id": None}
            )
            # Suppress the inner delegation's own activity events (on_activity=None): the voice's
            # voice_started already fired under the shared correlation id and exactly one voice_finished is
            # emitted by the outer delegate, so the activity table keeps one row per voice across the chain.
            recovered = await self.delegate(fb_req, correlation_id=correlation_id, base_depth=base_depth)
            alternate_attempts += recovered.delegation_call_count
            if recovered.ok:
                recovered.fallback_chain = failed_labels
                return recovered, alternate_attempts
            failed_labels.append(target.display_label)
        return None, alternate_attempts

    def _workspace_trusted(self, req: DelegationRequest) -> bool:
        """Whether a mutating delegation is permitted for ``req``'s working directory."""
        if req.trust_workspace:
            return True
        if not req.working_dir:
            return False
        try:
            target_dir = Path(req.working_dir).resolve()
        except OSError:
            return False
        for trusted in self._config.trusted_workspaces:
            try:
                root = Path(trusted).resolve()
            except OSError:
                continue
            if target_dir == root or target_dir.is_relative_to(root):
                return True
        return False

    def _should_persist(self, req: DelegationRequest) -> bool:
        """Whether this run should be kept as a durable job (F2); see ``RutherfordConfig.wants_persist``."""
        return self._config.wants_persist(req.persist)

    def _maybe_persist(self, req: DelegationRequest, result: DelegationResult, created_at: float) -> None:
        """Persist this run as a durable leaf job (F2) when the call opts in -- best-effort, in place.

        Resolution: an explicit ``req.persist`` wins; ``None`` follows the configured ``default_persistence``
        (Model A: ``ephemeral`` out of the box, so nothing is written unless asked). Nothing happens when
        persistence is off or no ledger is wired.

        Boundary: only a run that reached execution is recorded here. A run refused by an up-front guard
        (unknown target, depth, untrusted workspace) returns before the persist hook and is *not* persisted --
        the corpus is post-launch outcomes (success and runtime failure), not pre-flight refusals. The record
        pins the resolved launch ``argv`` (carried up on the result), the requested-vs-resolved model, the
        prompt/role/files/cwd, and the outcome (changed files + the sandbox diff for a write run), so the run
        recomposes from ``state.json`` alone. ``env`` is NEVER persisted (it can hold secrets). A filesystem
        failure is swallowed -- a run that already produced an answer must never fail because its record could
        not be written -- leaving the result without ``run_dir``. ``req.parent_run_id`` links a voice's record
        to its panel parent.
        """
        if not self._should_persist(req) or self._ledger is None:
            return
        record = RunRecord(
            run_id=uuid.uuid4().hex,
            kind="delegate",
            status=JobStatus.SUCCEEDED if result.ok else JobStatus.FAILED,
            created_at=created_at,
            finished_at=self._clock(),
            duration_s=result.duration_s,
            parent_run_id=req.parent_run_id,
            # item 9: the session this run produced (for a later continuation to resume) and the run this one
            # continues (the forward link), so the continuation chain is reconstructable from the records.
            session_id=result.session_id,
            continued_from=req.continues_run_id,
            cli=req.target.cli,
            requested_model=req.target.model,  # pre-fallback; result.target.model is the resolved one
            model=result.target.model,
            provenance=result.provenance,
            safety_mode=req.safety_mode,
            # F8a: the effort requested for this run and the tier the agent actually applied (post-clamp).
            requested_effort=result.effort,
            effort_applied=result.effort_applied,
            # F2: the resolved launch argv carried up on the result (None only when nothing launched).
            argv=list(result.argv) if result.argv is not None else [],
            cwd=req.working_dir or str(Path.cwd()),
            prompt=req.prompt,
            role=req.role,
            files=list(req.files),
            ok=result.ok,
            error_code=result.error.code if result.error is not None else None,
            # The sandbox already computed this run's per-delegation delta off HEAD (sound, not the dirty tree).
            changed_files=result.changed_files or [],
            cost=result.cost,
            # F8a: a leaf harvested at a panel budget carries its stop_reason so the child agrees with the
            # parent's harvest disposition. ``None`` on a clean finish.
            stop_reason=result.stop_reason,
            # N1 (item 3): a single delegation declares width 1; realized counts the subprocess delegations it
            # launched, INCLUDING a model fallback re-run (3-A). ``observed_peak_agents`` is the sampled peak.
            topology=Topology(
                declared=1,
                realized_delegations=result.delegation_call_count,
                observed_peak_agents=result.observed_peak_agents,
            ),
        )
        try:
            run_dir = self._ledger.write(record, answer=result.text, diff=result.diff)
        except Exception as exc:  # persistence is best-effort; never fail a produced answer over a bad write
            log_event("run_persist_failed", run_id=record.run_id, error_type=type(exc).__name__, error=str(exc))
            return
        result.run_dir = str(run_dir)


def _voice_finished_event(result: DelegationResult, role: str | None, depth: int, correlation_id: str) -> ActivityEvent:
    """Build the ``voice_finished`` :class:`ActivityEvent` for a finished delegation (N1, item 3).

    ``status`` distinguishes a clean ``ok`` from a ``cut`` (a time-budget harvest, ``stop_reason='budget'``)
    and a plain ``failed``, so the push side can colour the outcome without re-reading the result.
    ``correlation_id`` is the stable per-voice key so this terminal event collapses onto the same activity
    row as its ``voice_started``.
    """
    if result.ok:
        status = "ok"
    elif result.stop_reason == "budget":
        status = "cut"
    else:
        status = "failed"
    message = f"{result.target.display_label} {status}"
    if result.duration_s:
        message += f" ({result.duration_s:.1f}s)"
    return ActivityEvent(
        kind=ActivityEventKind.VOICE_FINISHED,
        correlation_id=correlation_id,
        cli=result.target.cli,
        model=result.target.model,
        role=role,
        status=status,
        elapsed_s=result.duration_s,
        observed_agents=result.observed_peak_agents,
        depth=depth,
        message=message,
    )


def _compose_prompt(prompt: str, files: list[str]) -> str:
    """Append an in-scope file list to the prompt (ACP resource blocks are a later refinement)."""
    if not files:
        return prompt
    listing = "\n".join(f"- {path}" for path in files)
    return f"{prompt}\n\nFiles in scope:\n{listing}"


def _fail(
    req: DelegationRequest, code: ErrorCode, message: str, *, details: dict[str, object] | None = None
) -> DelegationResult:
    """Build a failed result from an up-front guard, carrying the request's target and safety mode."""
    return DelegationResult(
        target=Target(cli=req.target.cli, model=req.target.model),
        ok=False,
        error=ErrorInfo(code=code, message=message, details=details),
        safety_mode=req.safety_mode,
    )


def _readonly_violated(req: DelegationRequest, result: DelegationResult) -> DelegationResult:
    """Turn an otherwise-ok read-only/propose result into a ``READONLY_VIOLATED`` failure (verify_read_only).

    The turn ran clean but the git fingerprint before/after the working_dir differs, so the agent broke its
    read-only promise. The successful answer is discarded for a loud failure -- a read-only delegation that
    mutated the tree is not a result to trust. ``SIDE_EFFECTED`` re-execution-safety records that a side effect
    occurred (never silently re-run elsewhere).
    """
    return DelegationResult(
        target=result.target,
        ok=False,
        duration_s=result.duration_s,
        error=ErrorInfo(
            code=ErrorCode.READONLY_VIOLATED,
            message=f"{req.safety_mode.value} delegation to {req.target.cli} modified its git working tree "
            "(verify_read_only); the agent did not keep the read-only promise",
            reexecution_safety=ReexecutionSafety.SIDE_EFFECTED,
        ),
        cost=result.cost,
        safety_mode=req.safety_mode,
        provenance=result.provenance,
        observed_peak_agents=result.observed_peak_agents,
    )


def _git_fingerprint(working_dir: str) -> str | None:
    """A fingerprint of the git tree under ``working_dir`` (status + staged/unstaged diffs), or ``None``.

    Combines ``git status --porcelain`` (catches a new, deleted, or renamed path -- including a gitignored
    write, via ``--ignored=matching``) with the unstaged and staged diffs (catches an in-place content edit to
    an already-tracked, already-dirty file that status alone would miss), all SCOPED to ``working_dir`` so an
    unrelated change elsewhere in the repo is not mis-attributed. ``None`` when ``working_dir`` is not a git
    repo or git is unavailable -- the caller then skips the check rather than failing a legitimate run.
    """
    status = _git_read(working_dir, "status", "--porcelain", "--ignored=matching", "--", ".")
    if status is None:
        return None
    unstaged = _git_read(working_dir, "diff", "--", ".") or ""
    staged = _git_read(working_dir, "diff", "--cached", "--", ".") or ""
    return f"{status}\n--unstaged--\n{unstaged}\n--staged--\n{staged}"


def _git_read(working_dir: str, *args: str) -> str | None:
    """Run a read-only git subcommand in ``working_dir``; return stdout, or ``None`` on any failure.

    Best-effort and never raising: a non-git dir, a missing git, or a non-zero exit all map to ``None`` so the
    ``verify_read_only`` fingerprint degrades to "could not check" rather than failing a clean delegation.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", working_dir, *args],
            capture_output=True,
            text=True,
            timeout=_FINGERPRINT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
