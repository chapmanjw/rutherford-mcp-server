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

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.client.connection import ClientSideConnection
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    PromptResponse,
    ResourceContentBlock,
    TextContentBlock,
)

from ..domain.enums import ReexecutionSafety
from ..domain.error_codes import ErrorCode
from ..domain.models import Cost, DelegationResult, ErrorInfo, Provenance, Target
from .client import RutherfordACPClient
from .descriptors import AgentDescriptor
from .journal import EventJournal, journal_event_from_message
from .launch import prepare_argv
from .permission import PermissionPolicy

#: How Rutherford identifies itself to an agent at ``initialize``.
_CLIENT_INFO = Implementation(name="rutherford-acp", version="3.0.0")
#: The handshake (initialize + new_session) gets a fixed bound; the model's thinking time is the prompt's.
_HANDSHAKE_TIMEOUT_S = 30.0

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
        self, descriptor: AgentDescriptor, *, policy: PermissionPolicy, cwd: str, model: str | None = None
    ) -> None:
        self._descriptor = descriptor
        self._policy = policy
        self._cwd = cwd
        self._target = Target(cli=descriptor.id, model=model or descriptor.default_model)
        self._journal = EventJournal()
        self._client = RutherfordACPClient(journal=self._journal, policy=policy, cwd=cwd)
        self._stack = AsyncExitStack()
        self._conn: ClientSideConnection | None = None
        self._session_id: str | None = None

    @property
    def target(self) -> Target:
        """The resolved ``(cli, model)`` this session answers under."""
        return self._target

    @property
    def session_id(self) -> str | None:
        """The agent's session id once opened, for provenance and a later resume; ``None`` before open."""
        return self._session_id

    async def __aenter__(self) -> ACPSession:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open(self) -> None:
        """Spawn the agent and complete the handshake, or raise :class:`ACPHandshakeError`."""
        env = _resolve_env(self._descriptor)
        command, *args = prepare_argv(self._descriptor.command)

        def _observe(event: StreamEvent) -> None:
            # SYNCHRONOUS observer: inline in receive order, so each turn's journal is complete before its
            # prompt response resolves. ``self._journal`` is swapped per turn, so this always writes the
            # current turn's journal.
            if event.direction is StreamDirection.INCOMING:
                entry = journal_event_from_message(event.message)
                if entry is not None:
                    self._journal.append(entry)

        try:
            conn, _process = await self._stack.enter_async_context(
                spawn_agent_process(
                    self._client,
                    command,
                    *args,
                    env=env,
                    cwd=self._cwd,
                    transport_kwargs={"stderr": None},
                    observers=[_observe],
                )
            )
        except FileNotFoundError as exc:
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.ACP_SPAWN_FAILED,
                f"could not launch {self._descriptor.id} ({command!r}): {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._conn = conn
        try:
            await asyncio.wait_for(
                conn.initialize(protocol_version=PROTOCOL_VERSION, client_info=_CLIENT_INFO),
                timeout=_HANDSHAKE_TIMEOUT_S,
            )
            session = await asyncio.wait_for(
                conn.new_session(cwd=self._cwd, mcp_servers=[]),
                timeout=_HANDSHAKE_TIMEOUT_S,
            )
        except Exception as exc:
            await self.close()
            raise ACPHandshakeError(
                ErrorCode.ACP_HANDSHAKE_FAILED,
                f"ACP handshake with {self._descriptor.id} failed: {exc}",
                ReexecutionSafety.SAFE,
            ) from exc
        self._session_id = session.session_id

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
        try:
            response = await asyncio.wait_for(
                self._conn.prompt(prompt=blocks, session_id=self._session_id),
                timeout=timeout_s,
            )
        except TimeoutError:
            await self.cancel()
            return _failed(
                self._target,
                self._policy,
                start,
                ErrorCode.ACP_TURN_TIMEOUT,
                f"{self._descriptor.id} did not finish within {timeout_s:.0f}s",
                _post_prompt_safety(self._journal),
                partial=self._journal.message_text() or None,
            )
        except Exception as exc:
            return _failed(
                self._target,
                self._policy,
                start,
                ErrorCode.ACP_TURN_ERROR,
                f"ACP turn for {self._descriptor.id} failed: {exc}",
                ReexecutionSafety.AMBIGUOUS,
            )
        return _reduce(self._descriptor, self._target, self._policy, self._journal, response, self._session_id, start)

    async def cancel(self) -> None:
        """Best-effort ``session/cancel`` for an in-flight turn; never raises."""
        if self._conn is not None and self._session_id is not None:
            with contextlib.suppress(Exception):
                await self._conn.cancel(session_id=self._session_id)

    async def close(self) -> None:
        """Tear down the agent connection (terminates the agent subprocess). Idempotent."""
        await self._stack.aclose()
        self._conn = None


async def run_acp_turn(
    descriptor: AgentDescriptor,
    prompt: str,
    *,
    policy: PermissionPolicy,
    cwd: str,
    timeout_s: float,
    model: str | None = None,
) -> DelegationResult:
    """Open a one-shot session, run a single prompt turn, and return the normalized result.

    The spawn-per-delegation path for ``delegate`` / ``consensus``. Never raises for an operational failure;
    a handshake/spawn failure becomes a failed :class:`DelegationResult` (re-execution-safe).
    """
    start = time.monotonic()
    try:
        async with ACPSession(descriptor, policy=policy, cwd=cwd, model=model) as session:
            return await session.prompt(prompt, timeout_s=timeout_s)
    except ACPHandshakeError as exc:
        target = Target(cli=descriptor.id, model=model or descriptor.default_model)
        return _failed(target, policy, start, exc.code, exc.message, exc.safety)


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
        duration_s=time.monotonic() - start,
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
        duration_s=time.monotonic() - start,
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
    """The environment for the agent subprocess: the full inherited env, or the descriptor's allowlist."""
    if descriptor.env_passthrough is None:
        return dict(os.environ)
    return {name: os.environ[name] for name in descriptor.env_passthrough if name in os.environ}
