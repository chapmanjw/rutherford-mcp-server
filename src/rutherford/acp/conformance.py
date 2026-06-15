# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""ACP conformance probing: does an agent actually drive over ACP on this machine?

The operational backbone for a heterogeneous roster. Each descriptor is probed with a trivial read-only
turn and classified -- working, installed-but-broken (handshake failed), or not installed (spawn failed) --
so the roster is a set of verified agents, not a list of optimistic launch commands. This is ``doctor``
over ACP: it is the only trustworthy signal, since there is no cheap non-interactive auth check.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time

from pydantic import BaseModel

from ..domain.enums import SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.models import DelegationResult
from .adapters import install_hint as adapter_install_hint
from .descriptors import AgentDescriptor
from .permission import PermissionPolicy
from .session import ACPHandshakeError, ACPSession, run_acp_turn

#: A trivial, side-effect-free prompt that any working agent should answer.
_PROBE_PROMPT = "Respond with exactly the word: OK"


class ConformanceReport(BaseModel):
    """The outcome of probing one agent's ACP server with a real round trip."""

    agent_id: str
    #: ``ok`` (handshake + answered) | ``no_answer`` (answered empty / refused) | ``handshake_failed``
    #: (installed, but initialize/new_session failed) | ``not_installed`` (the launch command was not found)
    #: | ``error`` (some other failure).
    status: str
    installed: bool
    answered: bool
    detail: str
    duration_s: float
    #: When ``status`` is ``not_installed`` but the agent's underlying CLI IS present (its npm ACP adapter
    #: shim is the only missing piece), the one-line ``npm i -g`` instruction to set it up; ``None`` otherwise.
    install_hint: str | None = None


def classify(agent_id: str, result: DelegationResult) -> ConformanceReport:
    """Map a probe turn's :class:`DelegationResult` to a :class:`ConformanceReport`."""
    if result.ok:
        return ConformanceReport(
            agent_id=agent_id,
            status="ok",
            installed=True,
            answered=True,
            detail="handshake + prompt round trip succeeded",
            duration_s=result.duration_s,
        )
    code = result.error.code if result.error is not None else None
    message = result.error.message if result.error is not None else "unknown failure"
    if code is ErrorCode.ACP_SPAWN_FAILED:
        return ConformanceReport(
            agent_id=agent_id,
            status="not_installed",
            installed=False,
            answered=False,
            detail=message,
            duration_s=result.duration_s,
        )
    if code is ErrorCode.ACP_HANDSHAKE_FAILED:
        return ConformanceReport(
            agent_id=agent_id,
            status="handshake_failed",
            installed=True,
            answered=False,
            detail=message,
            duration_s=result.duration_s,
        )
    if code in (ErrorCode.ACP_EMPTY_ANSWER, ErrorCode.ACP_REFUSED):
        return ConformanceReport(
            agent_id=agent_id,
            status="no_answer",
            installed=True,
            answered=False,
            detail=message,
            duration_s=result.duration_s,
        )
    return ConformanceReport(
        agent_id=agent_id, status="error", installed=True, answered=False, detail=message, duration_s=result.duration_s
    )


async def _probe_in(descriptor: AgentDescriptor, cwd: str, timeout_s: float) -> ConformanceReport:
    result = await run_acp_turn(
        descriptor, _PROBE_PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=cwd, timeout_s=timeout_s
    )
    report = classify(descriptor.id, result)
    # A not_installed for a wrapped-adapter agent whose underlying CLI IS present is really just a missing npm
    # shim -- surface the exact install command instead of a flat "not installed" the user can't act on.
    if report.status == "not_installed":
        hint = adapter_install_hint(descriptor)
        if hint is not None:
            return report.model_copy(update={"install_hint": hint, "detail": f"{report.detail} ({hint})"})
    return report


async def probe_agent(
    descriptor: AgentDescriptor, *, cwd: str | None = None, timeout_s: float = 60.0
) -> ConformanceReport:
    """Drive ``descriptor``'s agent with a trivial read-only turn and classify the outcome.

    By default the probe runs in an ISOLATED temp directory, not the user's workspace: a conformance check
    should not trigger an agent's heavyweight workspace setup against the real repo (OpenHands, for example,
    runs ``git fetch`` on a git cwd, which stalls the handshake). Pass ``cwd`` to probe a specific directory.
    """
    if cwd is not None:
        return await _probe_in(descriptor, cwd, timeout_s)
    # ignore_cleanup_errors: a working agent (e.g. codex-acp) can leave a grandchild process or open
    # handle holding the probe cwd for a moment after the session closes, and Windows refuses to delete a
    # directory still in use (WinError 32). The probe's job is to classify the agent, not to guarantee temp
    # cleanup -- so a residual temp dir is left for the OS to reap rather than crashing a successful probe.
    with tempfile.TemporaryDirectory(prefix="rutherford-acp-probe-", ignore_cleanup_errors=True) as probe_cwd:
        return await _probe_in(descriptor, probe_cwd, timeout_s)


class ConnectionReport(BaseModel):
    """The outcome of a handshake-only connection check: can Rutherford talk to and configure this agent?"""

    agent_id: str
    #: ``reachable`` (spawned, handshook, and opened a session) | ``handshake_failed`` (installed, but
    #: initialize/new_session failed) | ``not_installed`` (the launch command was not found) | ``error`` (an
    #: unexpected fault while opening).
    status: str
    installed: bool
    #: Whether the handshake completed and a session was opened (the "communicate + configure" signal).
    connected: bool
    #: The agent's session id once opened (proof a session was created), else ``None``.
    session_id: str | None
    #: The model ids the agent advertised at open (what you can configure it with); ``[]`` when it offers none.
    models: list[str]
    detail: str
    duration_s: float
    #: As on :class:`ConformanceReport`: the ``npm i -g`` instruction when a ``not_installed`` agent's
    #: underlying CLI is present and only its ACP adapter shim is missing; ``None`` otherwise.
    install_hint: str | None = None


async def probe_connection(
    descriptor: AgentDescriptor, *, cwd: str | None = None, timeout_s: float = 60.0
) -> ConnectionReport:
    """Open a session with ``descriptor``'s agent (handshake only, NO prompt) and report whether it connected.

    The lighter cousin of :func:`probe_agent`: it spawns the agent and completes the ACP
    ``initialize`` / ``new_session`` handshake -- proving Rutherford can *communicate with* and *configure*
    the agent (it captures the session id and the agent's advertised models) -- but never sends a prompt. So
    an agent that handshakes cleanly yet cannot complete a turn for a reason outside ACP (a model-side auth /
    entitlement / quota failure, e.g. Grok without a SuperGrok subscription) reports ``reachable`` here, where
    the full :func:`probe_agent` would report ``error`` on the turn. Like ``probe_agent`` it runs in an
    isolated temp directory by default; pass ``cwd`` to use a specific one.
    """
    if cwd is not None:
        return await _connect_in(descriptor, cwd, timeout_s)
    with tempfile.TemporaryDirectory(prefix="rutherford-acp-connect-", ignore_cleanup_errors=True) as connect_cwd:
        return await _connect_in(descriptor, connect_cwd, timeout_s)


async def _connect_in(descriptor: AgentDescriptor, cwd: str, timeout_s: float) -> ConnectionReport:
    """Open and immediately close a session, classifying the handshake outcome into a :class:`ConnectionReport`.

    ``timeout_s`` is the per-handshake-step budget (so a generous local-model floor reaches the handshake, not
    just the descriptor default). The session is ALWAYS torn down -- on the success path, on an unexpected
    fault, and on cancellation -- so a probe never leaks the spawned agent process: ``open()`` closes itself
    on an ``ACPHandshakeError``, and any other exit closes here before returning or propagating.
    """
    start = time.monotonic()
    session = ACPSession(
        descriptor, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=cwd, handshake_timeout_s=timeout_s
    )
    try:
        await session.open()
    except ACPHandshakeError as exc:
        # open() already tore the session down on failure. SPAWN_FAILED = not installed; anything else
        # (handshake / new_session) = installed but the handshake did not complete.
        installed = exc.code is not ErrorCode.ACP_SPAWN_FAILED
        return _connect_failure(
            descriptor, "not_installed" if not installed else "handshake_failed", installed, exc.message, start
        )
    except asyncio.CancelledError:
        # An OUTER cancel (e.g. the MCP client cancelling the doctor call) injected mid-open: open() may have
        # spawned the agent already, so tear it down before propagating -- never leak on cancellation.
        with contextlib.suppress(Exception):
            await session.close()
        raise
    except Exception as exc:  # open() is designed to raise only ACPHandshakeError; anything else is unexpected
        with contextlib.suppress(Exception):
            await session.close()
        return _connect_failure(descriptor, "error", True, f"unexpected error opening {descriptor.id}: {exc}", start)
    try:
        session_id, models = session.session_id, session.available_models
    finally:
        with contextlib.suppress(Exception):
            await session.close()  # always close a successfully-opened session; a teardown fault is non-fatal
    return ConnectionReport(
        agent_id=descriptor.id,
        status="reachable",
        installed=True,
        connected=True,
        session_id=session_id,
        models=models,
        detail="spawn + handshake + session open succeeded (no prompt sent)",
        duration_s=round(time.monotonic() - start, 3),
    )


def _connect_failure(
    descriptor: AgentDescriptor, status: str, installed: bool, detail: str, start: float
) -> ConnectionReport:
    """A non-reachable :class:`ConnectionReport` (the agent did not open a session)."""
    hint = adapter_install_hint(descriptor) if status == "not_installed" else None
    return ConnectionReport(
        agent_id=descriptor.id,
        status=status,
        installed=installed,
        connected=False,
        session_id=None,
        models=[],
        detail=f"{detail} ({hint})" if hint is not None else detail,
        duration_s=round(time.monotonic() - start, 3),
        install_hint=hint,
    )
