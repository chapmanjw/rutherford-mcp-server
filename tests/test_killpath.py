# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Kill-path / cancel / teardown coverage (roadmap item 6, rescoped for v3 ACP).

The v2-era gaps (SystemProbe timeout->kill, reparented-descendant residual, kill-on-spawn-fail) are N/A
under v3 (there is no ProcessRunner). v3 introduced its OWN cancel/teardown paths that the budget-cut and
timeout tests do NOT cover -- they assert Rutherford's bookkeeping (the result shape, the harvested
partial) but not that the agent was actually torn down. These close that gap:

1. ``PanelLifecycle.on_cancel`` -> exactly one terminal ``job_cancelled`` (N1, item 3, 3-K), and the
   start/closed guards (a cancel before start, or after a clean close, emits nothing) -- AND that a real
   running consensus/debate actually drives it (cancel a live panel, assert one ``job_cancelled``).
2. ``ACPSession.close()`` snapshots the agent's descendants BEFORE the transport tears down (Windows
   reparenting), shuts a brokered terminal, tears the transport down, then reaps -- the close-path ORDER.
3. ``ACPSession.open()`` tears the spawned agent down when a cancel lands DURING the handshake (a
   ``CancelledError`` is a ``BaseException``, so the per-stage ``except Exception`` guards miss it).
4. ``ACPSession.prompt`` issues ``session/cancel`` (the real ACP RPC) on a turn timeout.
5. ``_run_sandboxed`` cleans up a stranded sandbox when a cancel lands DURING the shielded open.

The reap PRIMITIVES (snapshot/reap killing a real process tree) are covered by ``test_teardown.py``; here
we cover the v3 paths that CALL them.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from rutherford.acp.client import TerminalBroker
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPSession
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import ActivityEventKind, JobStatus, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import (
    ActivityEvent,
    ConsensusRequest,
    DebateRequest,
    DelegationRequest,
    Target,
)
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import ActivityCallback, DelegationService, PanelLifecycle
from rutherford.services.jobs import JobStore

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
_READ_ONLY = PermissionPolicy(SafetyMode.READ_ONLY)
_TERMINALS = (ActivityEventKind.JOB_CANCELLED, ActivityEventKind.PANEL_FINISHED)


def _started(tool: str) -> ActivityEvent:
    return ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool=tool, depth=0)


def _arm_prompt_spy(monkeypatch: Any) -> asyncio.Event:
    """Return an event set the moment ANY ``ACPSession`` actually prompts its (spawned, handshaked) agent.

    A voice is only genuinely live -- the subprocess up and the HANG turn in flight -- once it reaches
    ``prompt``. ``VOICE_STARTED`` fires earlier (consensus emits it in ``delegate`` BEFORE the session spawns),
    so cancelling on it could land before any process exists; this fires after spawn + handshake, for both
    consensus (via ``run_acp_turn``) and debate (a direct session). So a cancel issued after it lands during
    real in-flight fanout, exercising the live teardown the test claims.
    """
    live = asyncio.Event()
    original = ACPSession.prompt

    async def prompt_spy(self: ACPSession, text: str, *, timeout_s: float) -> Any:
        live.set()
        return await original(self, text, timeout_s=timeout_s)

    monkeypatch.setattr(ACPSession, "prompt", prompt_spy)
    return live


# --- 1a. PanelLifecycle: the cancel terminal event (unit) --------------------


def test_panel_lifecycle_emits_one_job_cancelled_after_start() -> None:
    events: list[ActivityEvent] = []
    lifecycle = PanelLifecycle("consensus", 0, events.append)
    lifecycle.mark_started(_started("consensus"))
    lifecycle.on_cancel()
    assert [e.kind for e in events] == [ActivityEventKind.PANEL_STARTED, ActivityEventKind.JOB_CANCELLED]
    assert events[-1].status == "cut"  # the terminal cancel event closes the stream


def test_panel_lifecycle_cancel_is_gated_and_idempotent() -> None:
    # A cancel BEFORE the panel started emits nothing (no orphan terminal for a panel that never ran).
    before: list[ActivityEvent] = []
    PanelLifecycle("debate", 0, before.append).on_cancel()
    assert before == []
    # A cancel AFTER a clean close, or twice, emits nothing (exactly one terminal, never two).
    after: list[ActivityEvent] = []
    lifecycle = PanelLifecycle("debate", 0, after.append)
    lifecycle.mark_started(_started("debate"))
    lifecycle.mark_closed(ActivityEvent(kind=ActivityEventKind.PANEL_FINISHED, tool="debate", depth=0))
    lifecycle.on_cancel()
    lifecycle.on_cancel()
    assert [e.kind for e in after] == [ActivityEventKind.PANEL_STARTED, ActivityEventKind.PANEL_FINISHED]


# --- 1b. the WIRING: a real running panel drives on_cancel (end-to-end) -------
#
# The unit tests above pin the helper; these pin that consensus/debate actually CALL it. Without these, the
# ``except asyncio.CancelledError: lifecycle.on_cancel()`` block could be deleted from a panel service and the
# unit tests would stay green while a real cancelled panel stopped emitting its terminal.


async def _cancel_a_live_panel(coro: Any, voice_live: asyncio.Event) -> None:
    """Drive ``coro`` until a VOICE is in flight, cancel it, and require a clean ``CancelledError``.

    The wait is on ``voice_started`` (not ``panel_started``): a panel emits ``panel_started`` BEFORE it fans
    out, so cancelling on it would land before any voice subprocess is live. Waiting for the first voice means
    the cancel lands during real in-flight fanout -- the path that must still close the stream with one cut.
    """
    task = asyncio.create_task(coro)
    await asyncio.wait_for(voice_live.wait(), timeout=15.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_consensus_fanout_cancel_emits_exactly_one_job_cancelled(monkeypatch: Any) -> None:
    registry = DescriptorRegistry([FAKE])
    config = RutherfordConfig()
    service = ConsensusService(DelegationService(registry, config), registry, config)
    voice_live = _arm_prompt_spy(monkeypatch)
    events: list[ActivityEvent] = []
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="HANG forever",  # the voices sleep, so the panel is mid-flight when the cancel lands
        working_dir=str(REPO_ROOT),
    )
    await _cancel_a_live_panel(service.consensus(request, on_activity=events.append), voice_live)
    terminals = [e.kind for e in events if e.kind in _TERMINALS]
    assert terminals == [ActivityEventKind.JOB_CANCELLED]  # exactly one terminal, and it is the cancel


async def test_debate_fanout_cancel_emits_exactly_one_job_cancelled(monkeypatch: Any) -> None:
    registry = DescriptorRegistry([FAKE])
    config = RutherfordConfig()
    service = DebateService(registry, config, DelegationService(registry, config))
    voice_live = _arm_prompt_spy(monkeypatch)
    events: list[ActivityEvent] = []
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="HANG forever",
        rounds=2,
        working_dir=str(REPO_ROOT),
    )
    await _cancel_a_live_panel(service.debate(request, on_activity=events.append), voice_live)
    terminals = [e.kind for e in events if e.kind in _TERMINALS]
    assert terminals == [ActivityEventKind.JOB_CANCELLED]


# --- 1c. the async surface: cancel_job tears a running panel down ------------


async def test_cancel_job_cancels_a_running_panel_and_closes_its_stream(monkeypatch: Any) -> None:
    # The realistic async cancel path: cancel_job -> JobStore.cancel -> task.cancel() -> the panel's
    # ``except CancelledError: on_cancel()``. This ties the MCP surface to the terminal: the job ends CANCELLED
    # AND its buffered activity (what the ``activity`` tool serves) closes with exactly one job_cancelled.
    registry = DescriptorRegistry([FAKE])
    config = RutherfordConfig()
    service = ConsensusService(DelegationService(registry, config), registry, config)
    voice_live = _arm_prompt_spy(monkeypatch)
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="HANG forever",
        working_dir=str(REPO_ROOT),
    )

    async def factory(on_activity: ActivityCallback) -> str:
        await service.consensus(request, on_activity=on_activity)
        return "done"

    store = JobStore()
    job_id = await store.submit("consensus", factory)
    record = await store.get(job_id)
    await asyncio.wait_for(voice_live.wait(), timeout=15.0)  # the background panel has a live voice in flight
    await store.cancel(job_id)
    assert record.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await record.task  # let the cancellation unwind so the panel emits its terminal into the buffer
    assert record.status is JobStatus.CANCELLED
    terminals = [e.kind for e in record.activity if e.kind in _TERMINALS]
    assert terminals == [ActivityEventKind.JOB_CANCELLED]  # the poll buffer closes with exactly one cut


# --- 2. ACPSession.close(): snapshot -> shut terminals -> teardown -> reap ----


async def test_close_snapshots_before_teardown_then_shuts_terminals_then_reaps(monkeypatch: Any) -> None:
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    calls: list[str] = []

    def snapshot_spy(pid: int) -> list[int]:
        calls.append("snapshot")
        return [424242]  # a fake descendant so the reap branch is exercised and observable

    monkeypatch.setattr("rutherford.acp.session.snapshot_descendants", snapshot_spy)
    monkeypatch.setattr("rutherford.acp.session.reap", lambda pids: calls.append(f"reap:{pids}"))
    original_shutdown = session._client.shutdown_terminals

    async def shutdown_spy() -> None:
        calls.append("shutdown_terminals")
        await original_shutdown()

    monkeypatch.setattr(session._client, "shutdown_terminals", shutdown_spy)
    # Wrap the exit stack's teardown too, so the test pins reap-AFTER-transport-close, not just reap-last.
    original_aclose = session._stack.aclose

    async def aclose_spy() -> None:
        calls.append("transport_close")
        await original_aclose()

    monkeypatch.setattr(session._stack, "aclose", aclose_spy)
    await session.close()
    # The exact close-path order: snapshot the descendants BEFORE the transport teardown (a dead parent's
    # children reparent and drop out of the walk on Windows), shut the brokered terminal, tear the transport
    # down, and reap AFTER -- so a CLI a wrapper adapter fronts is killed, never orphaned in the working dir.
    assert calls == ["snapshot", "shutdown_terminals", "transport_close", "reap:[424242]"]


# --- 2b. a LIVE brokered terminal is actually killed on shutdown -------------


async def test_broker_shutdown_kills_a_live_terminal() -> None:
    # close() calls shutdown_terminals so a write-mode build/test the agent kicked off is killed rather than
    # orphaned in the sandbox. Test 2 pins that the call lands in the right order; this pins that the call
    # actually tears a LIVE process down (the reap primitive itself is covered by test_teardown.py).
    broker = TerminalBroker(REPO_ROOT)
    term_id = await broker.create(sys.executable, ["-c", "import time; time.sleep(30)"], None)
    process = broker._terminals[term_id].process
    try:
        assert process.poll() is None  # the command is genuinely running before shutdown
        await broker.shutdown()
        # wait() returns once the process is gone, or raises TimeoutExpired (a clean failure) if shutdown left
        # the live terminal alive -- so a regression that stopped killing brokered terminals turns this red.
        await asyncio.to_thread(process.wait, 5.0)
        assert process.returncode is not None  # the live terminal is dead after shutdown
    finally:
        if process.poll() is None:  # a kill-path test must never leak the very process it polices
            process.kill()


# --- 3. cancel DURING the handshake tears the spawned agent down -------------


async def test_open_cancel_during_handshake_tears_down_the_spawned_agent(monkeypatch: Any) -> None:
    # A cancel that lands while the handshake is in flight is a BaseException, so open()'s per-stage
    # ``except Exception`` guards do not catch it. Without the outer cancel guard the agent is spawned (and
    # registered on the exit stack) but never closed -- a leaked process tree, because run_acp_turn enters the
    # session with ``async with`` and Python skips ``__aexit__`` when ``open`` (``__aenter__``) raises.
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    in_handshake = threading.Event()
    block = asyncio.Event()
    closed: list[bool] = []
    original_close = session.close

    async def blocking_new_session(_conn: Any) -> None:
        in_handshake.set()  # the agent is spawned and the handshake has begun
        await block.wait()  # hang inside the handshake until the test cancels

    async def close_spy() -> None:
        closed.append(True)
        await original_close()

    monkeypatch.setattr(session, "_new_session", blocking_new_session)
    monkeypatch.setattr(session, "close", close_spy)
    task = asyncio.create_task(session.open())
    await asyncio.to_thread(in_handshake.wait, 10.0)
    assert session._pid is not None  # the agent really was spawned -- there is a live process to leak
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]  # the cancel during the handshake tore the spawned agent down


async def test_open_cancel_survives_a_transport_teardown_error(monkeypatch: Any) -> None:
    # close()'s "a teardown failure never propagates" contract is load-bearing here: open()'s cancel handler
    # calls close() before re-raising, so if close() let a transport teardown error escape it would MASK the
    # cancellation -- the task would surface the teardown error instead of CancelledError.
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    in_handshake = threading.Event()
    block = asyncio.Event()
    original_aclose = session._stack.aclose

    async def blocking_new_session(_conn: Any) -> None:
        in_handshake.set()
        await block.wait()

    async def teardown_then_raise() -> None:
        await original_aclose()  # do the real teardown (no leaked agent) ...
        raise RuntimeError("transport teardown blew up")  # ... then error, as a half-open generator can

    monkeypatch.setattr(session, "_new_session", blocking_new_session)
    task = asyncio.create_task(session.open())
    await asyncio.to_thread(in_handshake.wait, 10.0)
    monkeypatch.setattr(session._stack, "aclose", teardown_then_raise)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # the cancel still propagates; the teardown error did not mask it
        await task


# --- 4. turn timeout issues the real session/cancel RPC ----------------------


async def test_turn_timeout_issues_session_cancel(monkeypatch: Any) -> None:
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    try:
        cancelled: list[str] = []
        # Spy the actual ACP RPC (``_conn.cancel``), not the ``session.cancel`` wrapper -- so the test also
        # fails if cancel() were gutted to a no-op, proving the wire-level session/cancel really fired.
        original_cancel = session._conn.cancel  # type: ignore[union-attr]

        async def cancel_spy(*, session_id: str) -> None:
            cancelled.append(session_id)
            await original_cancel(session_id=session_id)

        monkeypatch.setattr(session._conn, "cancel", cancel_spy)
        result = await session.prompt("HANG forever", timeout_s=1.0)
        assert result.error is not None and result.error.code is ErrorCode.ACP_TURN_TIMEOUT
        assert cancelled == [session._session_id]  # the timeout issued session/cancel for THIS session
    finally:
        await session.close()


# --- 5. sandboxed open cancel cleans up the stranded sandbox -----------------


async def test_sandboxed_open_cancel_cleans_up_the_stranded_sandbox(monkeypatch: Any, tmp_path: Path) -> None:
    # A cancel that lands WHILE the worktree/copy is being built must not strand it: the shielded open is
    # awaited to recover the handle, cleaned up, then the cancel re-raised. Nothing tests this path otherwise.
    config = RutherfordConfig(trusted_workspaces=[str(tmp_path)])
    service = DelegationService(DescriptorRegistry([FAKE]), config)
    cleaned: list[bool] = []
    entered = threading.Event()

    class _SpySandbox:
        root = str(tmp_path)

        def cleanup(self) -> None:
            cleaned.append(True)

    def slow_open(cwd: str) -> _SpySandbox:
        entered.set()  # signal the open thread is in flight
        time.sleep(1.0)  # block so the cancel lands DURING the shielded open (the open runs off-thread, so the
        return _SpySandbox()  # shielded await cannot resolve while this sleeps -- the cancel is guaranteed mid-open)

    monkeypatch.setattr(service._sandbox, "open", slow_open)
    request = DelegationRequest(
        target=Target(cli="fake"),
        prompt="WRITE=x.txt:hi",
        safety_mode=SafetyMode.WRITE,
        trust_workspace=True,
        working_dir=str(tmp_path),
    )
    task = asyncio.create_task(service.delegate(request))
    assert await asyncio.to_thread(entered.wait, 10.0)  # the open thread started; a slow runner fails loudly here
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [True]  # the stranded sandbox was cleaned up despite the mid-open cancel
