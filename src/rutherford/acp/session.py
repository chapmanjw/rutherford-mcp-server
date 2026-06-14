# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Driving one ACP prompt turn end to end: the ACP-native delegation primitive.

:func:`run_acp_turn` spawns the agent as an ACP server, performs the ``initialize`` / ``new_session``
handshake, sends one ``session/prompt``, reduces the event journal into the normalized
:class:`~rutherford.domain.models.DelegationResult`, and classifies any failure's *re-execution safety* so
a fallback layer can decide whether a silent re-run is safe (the gate that protects against double cost or
double side effects). This one-shot turn is the spawn-per-delegation model used by ``delegate``/
``consensus``; a persistent session held across turns (``debate``) is a later layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
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
from .permission import PermissionPolicy

#: How Rutherford identifies itself to an agent at ``initialize``.
_CLIENT_INFO = Implementation(name="rutherford-acp", version="3.0.0")
#: The handshake (initialize + new_session) gets a fixed bound; the model's thinking time is the prompt's.
_HANDSHAKE_TIMEOUT_S = 30.0

#: The ACP prompt content-block union (annotated so the single-text-block list types cleanly).
PromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


async def run_acp_turn(
    descriptor: AgentDescriptor,
    prompt: str,
    *,
    policy: PermissionPolicy,
    cwd: str,
    timeout_s: float,
    model: str | None = None,
) -> DelegationResult:
    """Run one prompt turn against ``descriptor``'s agent and return the normalized result.

    Never raises for an operational failure: a spawn/handshake/timeout/refusal/transport error all become a
    failed :class:`DelegationResult` with an ACP error code and a re-execution-safety classification. Only
    an external cancellation (``CancelledError``) propagates.
    """
    target = Target(cli=descriptor.id, model=model or descriptor.default_model)
    journal = EventJournal()
    client = RutherfordACPClient(journal=journal, policy=policy, cwd=cwd)
    env = _resolve_env(descriptor)
    start = time.monotonic()
    command, *args = descriptor.command

    def _observe(event: StreamEvent) -> None:
        # SYNCHRONOUS observer: the SDK calls it inline in receive order, so every session/update is
        # journaled before the prompt response resolves the turn (the async handler would race it).
        if event.direction is StreamDirection.INCOMING:
            entry = journal_event_from_message(event.message)
            if entry is not None:
                journal.append(entry)

    try:
        async with spawn_agent_process(
            client, command, *args, env=env, cwd=cwd, transport_kwargs={"stderr": None}, observers=[_observe]
        ) as (conn, _process):
            try:
                await asyncio.wait_for(
                    conn.initialize(protocol_version=PROTOCOL_VERSION, client_info=_CLIENT_INFO),
                    timeout=_HANDSHAKE_TIMEOUT_S,
                )
                session = await asyncio.wait_for(
                    conn.new_session(cwd=cwd, mcp_servers=[]), timeout=_HANDSHAKE_TIMEOUT_S
                )
            except Exception as exc:
                return _failed(
                    target,
                    policy,
                    start,
                    ErrorCode.ACP_HANDSHAKE_FAILED,
                    f"ACP handshake with {descriptor.id} failed: {exc}",
                    ReexecutionSafety.SAFE,
                )
            session_id = session.session_id
            if model:
                with contextlib.suppress(Exception):
                    await conn.set_session_model(model_id=model, session_id=session_id)
            blocks: list[PromptBlock] = [text_block(prompt)]
            try:
                response = await asyncio.wait_for(conn.prompt(prompt=blocks, session_id=session_id), timeout=timeout_s)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    await conn.cancel(session_id=session_id)
                return _failed(
                    target,
                    policy,
                    start,
                    ErrorCode.ACP_TURN_TIMEOUT,
                    f"{descriptor.id} did not finish within {timeout_s:.0f}s",
                    _post_prompt_safety(journal),
                    partial=journal.message_text() or None,
                )
            return _reduce(descriptor, target, policy, journal, response, session_id, start)
    except FileNotFoundError as exc:
        return _failed(
            target,
            policy,
            start,
            ErrorCode.ACP_SPAWN_FAILED,
            f"could not launch {descriptor.id} ({command!r}): {exc}",
            ReexecutionSafety.SAFE,
        )
    except Exception as exc:
        return _failed(
            target,
            policy,
            start,
            ErrorCode.ACP_TURN_ERROR,
            f"ACP turn for {descriptor.id} failed: {exc}",
            ReexecutionSafety.AMBIGUOUS,
        )


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
