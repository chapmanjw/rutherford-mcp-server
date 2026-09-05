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
   reparenting), shuts brokered terminals, reaps descendants that can hold inherited pipes, then closes the
   transport -- the deadlock-safe close-path ORDER.
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
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import spawn_agent_process as sdk_spawn_agent_process

from rutherford.acp import teardown
from rutherford.acp.client import TerminalBroker
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPHandshakeError, ACPSession
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


# --- 2. ACPSession.close(): snapshot -> shut terminals -> reap -> teardown ----


async def test_close_snapshots_then_shuts_terminals_reaps_and_closes_transport(monkeypatch: Any) -> None:
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    calls: list[str] = []

    def snapshot_spy(pid: int) -> list[int]:
        calls.append("snapshot")
        return [424242]  # a fake descendant so the reap branch is exercised and observable

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants", snapshot_spy)
    monkeypatch.setattr("rutherford.acp.session.reap", lambda pids: calls.append(f"reap:{pids}"))
    original_shutdown = session._client.shutdown_terminals

    async def shutdown_spy() -> None:
        calls.append("shutdown_terminals")
        await original_shutdown()

    monkeypatch.setattr(session._client, "shutdown_terminals", shutdown_spy)
    # * Wrap transport teardown so the test pins descendant reap before any EOF-sensitive transport wait.
    original_aclose = session._stack.aclose

    async def aclose_spy() -> None:
        calls.append("transport_close")
        await original_aclose()

    monkeypatch.setattr(session._stack, "aclose", aclose_spy)
    # * Pin the adapter kill INSIDE the order too. Without it the assertion below still passes if the kill
    # drifts before the snapshot -- which would defeat the snapshot entirely, since descendants reparent
    # away the moment the adapter exits and are then invisible from its pid.
    original_kill = session._kill_direct_process

    async def kill_spy(process: Any) -> None:
        calls.append("kill_adapter")
        await original_kill(process)

    monkeypatch.setattr(session, "_kill_direct_process", kill_spy)
    await session.close()
    # * Snapshot before parent death, then kill holders of inherited stdio handles before transport EOF waits.
    assert calls == ["snapshot", "kill_adapter", "shutdown_terminals", "reap:[424242]", "transport_close"]


async def test_close_kills_direct_process_when_transport_teardown_ignores_eof(monkeypatch: Any) -> None:
    """A transport that never finishes must not hold the adapter alive -- and must not be cancelled either.

    This used to assert the transport close was CANCELLED at its deadline, which quietly made a real
    defect the specification. The SDK's ``Connection.close`` sets ``_closed`` before awaiting its
    dispatcher stop, sender close, and task shutdown, so a cancel in the middle strands the rest
    permanently: the flag is set, so every later close returns immediately. What actually has to hold is
    that ``close()`` RETURNS promptly and the adapter dies -- neither of which requires killing the
    cleanup that is still running.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    process = session._process
    assert process is not None and process.returncode is None
    original_aclose = session._stack.aclose
    transport_started = asyncio.Event()
    transport_cancelled = asyncio.Event()
    transport_finished = asyncio.Event()
    release = asyncio.Event()

    async def hanging_aclose() -> None:
        transport_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            transport_cancelled.set()
            raise
        transport_finished.set()

    monkeypatch.setattr("rutherford.acp.session._TRANSPORT_CLOSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(session._stack, "aclose", hanging_aclose)
    started = time.monotonic()
    await asyncio.wait_for(session.close(), timeout=0.5)
    elapsed = time.monotonic() - started

    assert transport_started.is_set()
    assert elapsed < 0.3  # the deadline bounded the WAIT ...
    return_code = await asyncio.wait_for(process.wait(), timeout=0.5)
    assert isinstance(return_code, int)  # ... and the adapter died regardless

    # ... while the close itself stayed free to finish once what it was waiting on arrived. Asserted by
    # letting it COMPLETE rather than by reading the cancelled flag here: `cancel()` only requests, so the
    # flag lags the call and a check at this point passes or fails on scheduling luck.
    release.set()
    try:
        await asyncio.wait_for(transport_finished.wait(), timeout=1.0)
    except TimeoutError:
        raise AssertionError(
            "the transport close was cancelled at its deadline, so the dispatcher stop, sender close and "
            "task shutdown after it can never run -- the SDK has already latched _closed"
        ) from None
    assert not transport_cancelled.is_set()
    await original_aclose()


async def test_a_real_transport_close_that_overruns_its_deadline_still_drains(monkeypatch: Any) -> None:
    """The same guarantee against the REAL SDK, not a stub -- and it must not stay pinned afterwards.

    The test above proves a synthetic ``aclose`` is not cancelled. It cannot prove the production claim the
    whole design rests on: that the real ``Connection.close`` actually FINISHES once the adapter is dead, so
    declining to cancel it costs a brief retention rather than a permanent one. Without this, a future SDK
    change could turn the un-cancelled task into a leak while the stubbed test stayed green.

    The real close is held behind a gate so the deadline is guaranteed to expire first, then released to run
    for real. Merely shrinking the deadline to zero does NOT work and silently proves nothing: ``asyncio.wait``
    still yields, the close against a fake agent finishes inside that window, and its release callback has not
    run yet -- so the task looks pending-and-uncancelled whether or not it was ever going to be cancelled.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    original_aclose = session._stack.aclose
    gate = asyncio.Event()
    captured: list[asyncio.Task[Any]] = []

    async def gated_real_aclose() -> None:
        # * Grab the task from INSIDE it. Sampling the owner set after `close` returns cannot work: a
        # cancelled task is already gone by then, so the sample comes back empty and the failure reads as
        # "nothing to check" instead of "it was cancelled" -- the wrong diagnosis for the right bug.
        running = asyncio.current_task()
        assert running is not None
        captured.append(running)
        await gate.wait()  # outlast the deadline ...
        await original_aclose()  # ... then let the REAL SDK teardown run

    monkeypatch.setattr(session._stack, "aclose", gated_real_aclose)
    monkeypatch.setattr("rutherford.acp.session._TRANSPORT_CLOSE_TIMEOUT_S", 0.05)

    await asyncio.wait_for(session.close(), timeout=15.0)

    assert captured, "the transport close never started, so this test proved nothing"
    task = captured[0]
    owned_while_pending = task in teardown._PENDING_CLEANUPS

    gate.set()  # the deadline has passed; now let the real close proceed
    done, _ = await asyncio.wait({task}, timeout=10.0)
    assert task in done, (
        "a real transport close never finished, so it stays pinned in the shared owner for the life of the "
        "process -- declining to cancel it is only safe because the dead adapter lets it complete"
    )
    assert not task.cancelled(), "the real transport close was cancelled -- the SDK cannot resume it"
    assert owned_while_pending, "an overrunning transport close was left with no owner"
    await asyncio.sleep(0)  # let the release callbacks run
    assert task not in teardown._PENDING_CLEANUPS, "a finished transport close kept its slot"


async def test_a_close_task_outliving_its_caller_still_has_an_owner(monkeypatch: Any) -> None:
    """The aggregate teardown needs an owner for the same reason its stages do.

    The caller budget is deliberately allowed to be shorter than the stages it covers, so ``close`` can
    return while ``_close_body`` is still going. In that window the session attribute was the only thing
    holding the task, and the loop holds tasks weakly -- so a session dropped right there could take its own
    teardown with it. Every stage inside already had a durable owner; the thing wrapping them did not.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    release = asyncio.Event()
    original_aclose = session._stack.aclose

    async def slow_aclose() -> None:
        await release.wait()
        await original_aclose()

    monkeypatch.setattr(session._stack, "aclose", slow_aclose)
    monkeypatch.setattr("rutherford.acp.session._TRANSPORT_CLOSE_TIMEOUT_S", 30.0)
    monkeypatch.setattr("rutherford.acp.session._SESSION_CLOSE_WAIT_S", 0.05)

    await asyncio.wait_for(session.close(), timeout=5.0)
    close_task = session._close_task
    assert close_task is not None and not close_task.done(), (
        "the body finished within the caller budget, so this test never entered the window it is about"
    )
    # * What this asserts is the ownership, which is checkable. The collection it prevents is not: forcing a
    # GC of a live task mid-await and observing the loss is not something a test can stage reliably, so the
    # consequence stays an inference from asyncio's documented weak task references rather than a claim this
    # test demonstrates.
    assert close_task in teardown._PENDING_CLEANUPS, (
        "a close task outliving its caller has no durable owner -- the session attribute alone holds it, and "
        "the event loop keeps only a weak reference, so nothing stops it being collected mid-flight"
    )

    release.set()
    await asyncio.wait_for(asyncio.shield(close_task), timeout=10.0)


async def test_close_continues_after_its_waiter_is_cancelled(monkeypatch: Any) -> None:
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()
    transport_finished = asyncio.Event()
    calls = 0

    async def slow_aclose() -> None:
        nonlocal calls
        calls += 1
        transport_started.set()
        await release_transport.wait()
        transport_finished.set()

    monkeypatch.setattr(session._stack, "aclose", slow_aclose)
    waiter = asyncio.create_task(session.close())
    await asyncio.wait_for(transport_started.wait(), timeout=0.5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release_transport.set()
    await asyncio.wait_for(session.close(), timeout=0.5)
    assert transport_finished.is_set()
    assert calls == 1


async def test_descendant_snapshot_starts_while_default_executor_is_saturated(monkeypatch: Any) -> None:
    """The pre-kill snapshot gets its own thread, so a full executor cannot delay it past the adapter's death."""
    snapshot_started = threading.Event()
    release = threading.Event()

    def snapshot_spy(pid: int) -> list[Any]:
        assert pid == 12345
        snapshot_started.set()
        return []

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants", snapshot_spy)
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    session._pid = 12345

    with _single_worker_executor() as hold:
        hold(release)
        await asyncio.wait_for(session.close(), timeout=0.5)
        assert snapshot_started.is_set()
        release.set()


async def test_cancel_returns_when_agent_ignores_session_cancel(monkeypatch: Any) -> None:
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    cancel_started = asyncio.Event()
    cancel_cancelled = asyncio.Event()
    never = asyncio.Event()

    async def hanging_cancel(*, session_id: str) -> None:
        assert session_id == session._session_id
        cancel_started.set()
        try:
            await never.wait()
        finally:
            cancel_cancelled.set()

    try:
        monkeypatch.setattr("rutherford.acp.session._CANCEL_TIMEOUT_S", 0.05)
        monkeypatch.setattr(session._conn, "cancel", hanging_cancel)
        started = time.monotonic()
        await asyncio.wait_for(session.cancel(), timeout=0.5)
        elapsed = time.monotonic() - started

        assert cancel_started.is_set()
        assert elapsed < 0.3
        await asyncio.wait_for(cancel_cancelled.wait(), timeout=0.5)
    finally:
        await session.close()


async def test_agent_stderr_is_owned_not_inherited_from_the_mcp_host(monkeypatch: Any) -> None:
    """The agent's stderr must be a pipe Rutherford owns -- never the MCP host's inherited fd 2.

    This replaces an assertion that pinned ``DEVNULL``. The invariant it was really protecting is "the child
    cannot write into a descriptor the host owns and nobody drains", which is what ``stderr=None`` (inherit)
    would do and what once deadlocked the host. ``DEVNULL`` satisfied that by discarding -- including the one
    line a launch failure explains itself with -- so the pipe is now owned and drained instead.

    The flag alone is a weak invariant: ``PIPE`` with no reader would pass it. The companion test below is the
    half that proves the capture actually happens, so change them as a pair.
    """
    captured: list[dict[str, Any]] = []

    def spawn_spy(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return sdk_spawn_agent_process(*args, **kwargs)

    monkeypatch.setattr("rutherford.acp.session.spawn_agent_process", spawn_spy)
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    await session.close()

    assert captured
    stderr_arg = captured[0]["transport_kwargs"]["stderr"]
    assert stderr_arg == subprocess.PIPE
    # The regression that matters: None means "inherit the parent's fd 2".
    assert stderr_arg is not None


async def test_agent_stderr_reaches_the_handshake_failure_detail() -> None:
    """A child that explains itself on stderr and dies must have said so in the error an operator reads.

    This is the whole point of the change. The diagnosed failure -- a launcher shim handed a name it does not
    recognize -- prints one exact line and exits before reading a byte of stdin, which ACP can otherwise only
    surface as an instant "Connection closed": a description of the socket, not of the cause.
    """
    marker = "STDERR-MARKER-launcher-rejected-the-name"
    loud = AgentDescriptor(
        "loud",
        "Loud",
        (sys.executable, "-c", f"import sys; sys.stderr.write({marker!r}); sys.stderr.flush(); sys.exit(1)"),
    )
    session = ACPSession(loud, policy=_READ_ONLY, cwd=str(REPO_ROOT), handshake_timeout_s=10.0)

    with pytest.raises(ACPHandshakeError) as excinfo:
        await session.open()

    assert excinfo.value.code is ErrorCode.ACP_HANDSHAKE_FAILED
    assert marker in excinfo.value.message
    assert "agent stderr:" in excinfo.value.message


async def test_agent_stderr_detail_strips_terminal_escape_sequences() -> None:
    """Agent-authored stderr is rendered in the operator's terminal, so escapes must not survive.

    OSC in particular can retitle a window, forge a hyperlink (OSC 8), or write the clipboard (OSC 52). The
    payload here carries an OSC clipboard write and a CSI colour run around the text worth keeping.
    """
    payload = "\\x1b]52;c;cGF5bG9hZA==\\x07\\x1b[31mreal failure line\\x1b[0m"
    hostile = AgentDescriptor(
        "hostile",
        "Hostile",
        (sys.executable, "-c", f'import sys; sys.stderr.write("{payload}"); sys.stderr.flush(); sys.exit(1)'),
    )
    session = ACPSession(hostile, policy=_READ_ONLY, cwd=str(REPO_ROOT), handshake_timeout_s=10.0)

    with pytest.raises(ACPHandshakeError) as excinfo:
        await session.open()

    message = excinfo.value.message
    assert "real failure line" in message
    assert "\x1b" not in message and "\x07" not in message
    assert "52;c;" not in message


async def test_agent_stderr_capture_is_bounded_and_does_not_stall_the_handshake() -> None:
    """A flood on stderr must neither wedge the handshake nor be retained without bound.

    The child writes far past the retention cap with no newlines -- the shape that breaks a ``readline``-based
    reader -- then dies. The drain has to keep consuming past the cap, or the child blocks on a full pipe and
    the failure that should take milliseconds takes the whole handshake budget.
    """
    flood = AgentDescriptor(
        "flood",
        "Flood",
        (sys.executable, "-c", "import sys; sys.stderr.write('x' * 2_000_000); sys.stderr.flush(); sys.exit(1)"),
    )
    session = ACPSession(flood, policy=_READ_ONLY, cwd=str(REPO_ROOT), handshake_timeout_s=30.0)

    start = time.monotonic()
    with pytest.raises(ACPHandshakeError) as excinfo:
        await session.open()
    elapsed = time.monotonic() - start

    assert elapsed < 20.0, "the drain did not keep the pipe clear"
    # Surfaced text stays bounded even though 2 MB was written.
    assert len(excinfo.value.message) < 8000


# --- 2b. a LIVE brokered terminal is actually killed on shutdown -------------


async def test_broker_shutdown_starts_all_terminal_kills_concurrently(monkeypatch: Any) -> None:
    broker = TerminalBroker(REPO_ROOT)
    started: list[str] = []
    all_started = asyncio.Event()
    release = asyncio.Event()

    class _SlowTerminal:
        def __init__(self, name: str) -> None:
            self._name = name

        async def kill(self) -> None:
            started.append(self._name)
            if len(started) == 2:
                all_started.set()
            await release.wait()

    monkeypatch.setattr(
        broker,
        "_terminals",
        {"first": _SlowTerminal("first"), "second": _SlowTerminal("second")},
    )
    shutdown = asyncio.create_task(broker.shutdown())
    await asyncio.wait_for(all_started.wait(), timeout=0.5)
    release.set()
    await asyncio.wait_for(shutdown, timeout=0.5)

    assert set(started) == {"first", "second"}
    assert not broker._terminals


async def test_broker_shutdown_cancellation_retains_terminal_until_shared_kill_finishes(monkeypatch: Any) -> None:
    broker = TerminalBroker(REPO_ROOT)
    term_id = await broker.create(sys.executable, ["-c", "import time; time.sleep(5)"], None)
    terminal = broker._terminals[term_id]
    process = terminal.process
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def slow_snapshot(pid: int) -> list[Any]:
        assert pid == process.pid
        snapshot_started.set()
        await release_snapshot.wait()
        return []

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants_eagerly", slow_snapshot)
    shutdown = asyncio.create_task(broker.shutdown())
    await asyncio.wait_for(snapshot_started.wait(), timeout=0.5)
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert broker._terminals.get(term_id) is terminal
    release_snapshot.set()
    await asyncio.wait_for(broker.shutdown(), timeout=0.5)
    await asyncio.to_thread(process.wait, 2.0)
    assert process.returncode is not None
    assert term_id not in broker._terminals


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


# --- teardown: the snapshot stage must be bounded like every other stage ---------------------------------


async def test_a_hung_snapshot_still_kills_the_adapter(monkeypatch: Any) -> None:
    """Every teardown stage carries a deadline; the snapshot must not be the exception.

    The snapshot runs first and its whole point is to enumerate descendants while the adapter is still
    alive. If it hangs and is unbounded, the body never reaches the kill AND the `finally` never runs --
    a `finally` only fires when its block exits -- so the adapter survives for the life of the server.
    That is precisely the leak this teardown path exists to prevent, reached through the one stage that
    was not bounded.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    process = session._process
    assert process is not None

    wedged = threading.Event()
    # * Released in `finally` rather than slept through. A thread still running at teardown keeps
    # executing measured lines after coverage stops, which is what made the per-file floor
    # load-sensitive; the test must own its thread's lifetime, not outlive it.
    release = threading.Event()

    def hanging_snapshot(pid: int) -> list[int]:
        wedged.set()
        release.wait(timeout=30)  # blocks like a wedged enumeration, but ends when the test says so
        return []

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants", hanging_snapshot)
    monkeypatch.setattr("rutherford.acp.session._SNAPSHOT_TIMEOUT_S", 0.05)
    monkeypatch.setattr("rutherford.acp.session._SESSION_CLOSE_WAIT_S", 5.0)

    try:
        await session.close()

        assert wedged.is_set()  # the snapshot really did run and really did hang
        # The adapter must be dead despite it: the stage timed out and teardown carried on.
        await asyncio.wait_for(process.wait(), timeout=5.0)
        assert process.returncode is not None
    finally:
        release.set()
        await asyncio.sleep(0)  # let the freed thread finish before the test returns


async def test_a_hung_terminal_snapshot_still_kills_the_command(monkeypatch: Any) -> None:
    """The brokered-terminal kill needs the same bound as session teardown.

    ``_kill_body`` snapshots before killing for the same reason ``_close_body`` does, and it had the same
    flaw: an unbounded await means the ``try`` never exits, so the ``finally`` that kills the process never
    runs. The blast radius differs -- a brokered terminal is a child of the SERVER, not the adapter, so the
    session's reap walks a different tree entirely and never collects it. The result is a permanent orphan
    holding the sandbox cwd, which is the failure this module exists to prevent.
    """
    broker = TerminalBroker(REPO_ROOT)
    term_id = await broker.create(sys.executable, ["-c", "import time; time.sleep(30)"], None)
    terminal = broker._terminals[term_id]
    process = terminal.process
    snapshot_started = asyncio.Event()

    release = asyncio.Event()

    async def wedged_snapshot(pid: int) -> list[Any]:
        snapshot_started.set()
        await release.wait()  # blocks past the deadline, but the test owns when it ends
        return []

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants_eagerly", wedged_snapshot)
    monkeypatch.setattr("rutherford.acp.client._TERMINAL_SNAPSHOT_TIMEOUT_S", 0.05)

    await asyncio.wait_for(terminal.kill(), timeout=10.0)

    assert snapshot_started.is_set()  # the snapshot really did run and really did wedge
    assert process.poll() is not None  # and the command died anyway

    release.set()  # leave no pending coroutine behind for the loop teardown to complain about
    await asyncio.sleep(0)


async def test_a_late_snapshot_is_reaped_not_discarded(monkeypatch: Any) -> None:
    """A snapshot that lands after the deadline still captured a real tree; dropping it leaks that tree.

    The deadline guarantees the adapter dies -- it does not assert the tree is empty. Nothing sweeps
    these later: the next probe spawns a new adapter with a new pid and never walks this one again.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    reaped: list[list[int]] = []
    reap_called = threading.Event()

    def reap_spy(procs: list[int], **_: Any) -> None:
        reaped.append(procs)
        reap_called.set()

    release = asyncio.Event()

    async def slow_snapshot(pid: int) -> list[int]:
        await release.wait()  # lands well after the deadline below
        return [515151]

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants_eagerly", slow_snapshot)
    monkeypatch.setattr("rutherford.acp.teardown.reap", reap_spy)
    monkeypatch.setattr("rutherford.acp.session._SNAPSHOT_TIMEOUT_S", 0.05)

    await session.close()  # returns having timed out the snapshot
    assert reaped == []  # nothing reaped yet -- the snapshot has not finished

    release.set()  # now let the late snapshot complete
    await asyncio.wait_for(asyncio.to_thread(reap_called.wait, 5.0), timeout=10.0)

    assert reaped == [[515151]]  # the late tree was handed off and reaped, not dropped


async def test_a_snapshot_timeout_is_announced(monkeypatch: Any) -> None:
    """A timed-out snapshot must be distinguishable from a genuinely empty tree.

    The caller budget is squeezed too, so a regression here fails in a moment rather than sitting on the
    full close budget first -- a slow failure gets read as a hang and investigated as the wrong problem.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "rutherford.acp.teardown.log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()

    async def never(pid: int) -> list[int]:
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants_eagerly", never)
    monkeypatch.setattr("rutherford.acp.session._SNAPSHOT_TIMEOUT_S", 0.05)
    monkeypatch.setattr("rutherford.acp.session._SESSION_CLOSE_WAIT_S", 2.0)

    await session.close()

    assert any(name == "acp_teardown_snapshot_timeout" for name, _ in events), (
        f"the timeout was silent; got {[n for n, _ in events]}"
    )


async def test_a_cancelled_bounded_cleanup_retains_its_task_and_propagates() -> None:
    """Cancelling the WAITER must not abandon the work it was waiting on.

    A cancel landing inside the `asyncio.wait` has to leave the underlying task with an owner before
    re-raising -- otherwise teardown work is dropped on the floor. Exercised directly rather than by racing
    a real close, because whether the cancel lands during that await is a coin flip, and a branch that is
    only sometimes covered is only sometimes correct.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    started = asyncio.Event()
    before = set(teardown._PENDING_CLEANUPS)

    async def slow_work() -> str:
        started.set()
        await asyncio.sleep(30)
        return "done"

    async def call() -> str:
        return await session._bounded_cleanup(
            slow_work(),
            timeout_s=30.0,
            task_name="rutherford-test-cleanup",
            default="default",
            cancel_on_timeout=True,
        )

    waiter = asyncio.create_task(call())
    await asyncio.wait_for(started.wait(), timeout=5.0)
    # * Captured while the work is still in flight: the set is self-emptying, so reading it after the
    # cancellation lands would find it gone for the RIGHT reason and prove nothing about ownership.
    registered = set(teardown._PENDING_CLEANUPS) - before
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert len(registered) == 1, "the inner task was dropped instead of handed to the shared owner"
    inner = registered.pop()
    await asyncio.sleep(0)  # let the cancellation the waiter requested actually land
    assert inner.cancelled(), "cancel_on_timeout=True must stop the work, not leave it running"
    assert inner not in teardown._PENDING_CLEANUPS, "a finished task must release its slot"


async def test_the_session_reap_survives_a_saturated_executor() -> None:
    """The session's reap gets the same guarantee as the terminal's -- proven, not asserted by symmetry.

    The two paths kept one retention scheme each and drifted; the session's capped set silently stopped
    backing tasks past its cap, so a busy teardown could drop the very reap its deadline had promised to
    let finish. They share one owner now, and this drives the SESSION entry point under the same
    deterministic saturation the terminal test uses, so a future divergence fails here rather than in
    production.
    """
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    reaped = threading.Event()
    release = threading.Event()

    with _single_worker_executor() as hold:
        hold(release)
        result = await session._bounded_cleanup(
            asyncio.to_thread(lambda: reaped.set()),
            timeout_s=0.05,  # expires while the work is still queued behind the hog
            task_name="rutherford-test-session-reap",
            default="expired",
            cancel_on_timeout=False,
        )
        assert result == "expired", "the deadline should have expired -- the test is not exercising saturation"
        assert not reaped.is_set()
        release.set()
        assert await asyncio.to_thread(reaped.wait, 10.0), (
            "the session reap was cancelled instead of merely un-awaited: captured work was dropped"
        )


async def test_a_backlog_of_wedged_cleanups_is_reported(monkeypatch: Any) -> None:
    """An uncapped owner is only defensible if a growing backlog is visible.

    Capping it would mean refusing to hold a reference, and an unreferenced task is one the loop may
    collect mid-flight -- the same drop these deadlines exist to prevent, relocated rather than removed.
    So the backlog is allowed to grow and is reported instead. If that report ever goes silent, an
    executor wedge becomes invisible and the case for leaving it uncapped collapses.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "rutherford.acp.teardown.log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    release = asyncio.Event()

    async def wedged() -> None:
        await release.wait()

    # * Adding this many consecutive entries crosses a multiple of the warn step no matter what the set
    # already held, so the assertion does not depend on other tests having drained it.
    wedges = [
        asyncio.create_task(wedged(), name=f"rutherford-test-wedge-{index}")
        for index in range(teardown._BACKLOG_WARN_EVERY)
    ]
    try:
        for task in wedges:
            teardown.register_pending_cleanup(task)
        assert any(name == "acp_teardown_cleanup_backlog" for name, _ in events), (
            f"a wedged backlog was silent; got {sorted({n for n, _ in events})}"
        )
    finally:
        release.set()
        await asyncio.gather(*wedges, return_exceptions=True)

    assert all(task not in teardown._PENDING_CLEANUPS for task in wedges), "finished cleanups must free their slots"


async def test_a_backlog_sitting_on_the_step_boundary_reports_once(monkeypatch: Any) -> None:
    """The report fires per step reached, not per teardown that touches a step boundary.

    A plain "every Nth entry" test looks equivalent and is not: a backlog parked on a boundary that loses
    and regains one task re-fires on every other registration, turning a signal that teardown is wedged
    into a stream that gets muted. Guarding an uncapped set with a warning is only defensible while the
    warning stays worth reading.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "rutherford.acp.teardown.log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    # * Both halves of the module state are pinned, not just the level. An earlier draft of this test only
    # caught the never-falling-mark case because another test happened to run first and reset it -- a test
    # that passes for a reason it does not state is one that stops working when the suite is reordered or
    # filtered. Pinning the level alone leaves the same exposure through the set: it is empty here only
    # because every cleanup so far has completed, and one leaked pending task elsewhere would shift every
    # count below.
    monkeypatch.setattr(teardown, "_backlog_reported_at", 0)
    monkeypatch.setattr(teardown, "_PENDING_CLEANUPS", set())
    release = asyncio.Event()

    async def wedged() -> None:
        await release.wait()

    def make(kind: str, count: int) -> list[asyncio.Task[None]]:
        return [asyncio.create_task(wedged(), name=f"rutherford-test-{kind}-{index}") for index in range(count)]

    def backlogs() -> int:
        return sum(1 for name, _ in events if name == "acp_teardown_cleanup_backlog")

    async def finish(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # * One BELOW the step, so each churned task takes the level to exactly the step and back. That is the
    # crossing a modulo rule cannot tell apart from a fresh one -- parking AT the step and adding one more
    # would behave identically under either rule and prove nothing.
    held = make("hover", teardown._BACKLOG_WARN_EVERY - 1)
    churn = make("churn", 4)
    again = make("again", teardown._BACKLOG_WARN_EVERY)
    try:
        for task in held:
            teardown.register_pending_cleanup(task)
        assert backlogs() == 0, "a backlog below the first step should say nothing"

        for task in churn:
            teardown.register_pending_cleanup(task)  # -> exactly the step
            await finish([task])  # -> back below it
        assert backlogs() == 1, f"the step was reported {backlogs()} times while hovering on its boundary"

        # * Now drain and climb again. The mark has to fall with the set, or one early spike mutes the
        # warning for the life of the process -- which would make the uncapped set genuinely indefensible.
        await finish(held)
        for task in again:
            teardown.register_pending_cleanup(task)
        assert backlogs() == 2, "after draining, reaching the step again must report -- the mark did not fall"
    finally:
        release.set()
        await asyncio.gather(*held, *churn, *again, return_exceptions=True)


async def test_an_undispatched_late_reap_is_reported_as_dropped(monkeypatch: Any) -> None:
    """A tree that could not be handed to a thread must be logged as dropped, never as collected.

    The reap is dispatched before it is reported precisely so this case stays distinguishable.
    ``Thread.start`` raises ``RuntimeError`` for resource exhaustion as much as for interpreter
    finalization, so a log written before the start would be lying on a live server, not only at
    shutdown -- and a dropped tree recorded as collected is the one failure nobody goes looking for.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "rutherford.acp.teardown.log_event",
        lambda event, **fields: events.append((event, fields)),
    )

    class _Exhausted:
        def __init__(self, **_: Any) -> None: ...

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    # * Swap the module's own `threading` reference rather than the real `threading.Thread`, so a test about
    # thread exhaustion does not actually stop the rest of the process from starting threads.
    monkeypatch.setattr("rutherford.acp.teardown.threading", SimpleNamespace(Thread=_Exhausted))

    async def captured() -> list[int]:
        return [909090]

    task: Any = asyncio.create_task(captured())
    await task
    teardown._reap_late_snapshot(task, source="session")

    names = [name for name, _ in events]
    assert "acp_teardown_late_snapshot_scheduled" not in names, "an undispatched tree was reported as collected"
    dropped = [fields for name, fields in events if name == "acp_teardown_late_snapshot_dropped"]
    assert len(dropped) == 1, f"the drop was silent; got {names}"
    assert dropped[0]["descendants"] == 1
    assert "can't start new thread" in dropped[0]["reason"], "the report must carry why it could not be dispatched"


async def test_a_late_terminal_snapshot_is_reaped_not_discarded(monkeypatch: Any) -> None:
    """The mirror of the session test, for the path that had it missing.

    Bounding the terminal snapshot guaranteed the command dies but still threw away a tree that arrived
    late -- and nothing else collects it, because a brokered terminal is a child of the server rather
    than the adapter. Both paths now share one helper precisely so this cannot diverge again.
    """
    broker = TerminalBroker(REPO_ROOT)
    term_id = await broker.create(sys.executable, ["-c", "import time; time.sleep(30)"], None)
    terminal = broker._terminals[term_id]
    reaped: list[list[int]] = []
    reap_called = threading.Event()

    def reap_spy(procs: list[int], **_: Any) -> None:
        reaped.append(procs)
        reap_called.set()

    release = asyncio.Event()

    async def slow_snapshot(pid: int) -> list[int]:
        await release.wait()  # lands after the deadline below
        return [626262]

    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants_eagerly", slow_snapshot)
    monkeypatch.setattr("rutherford.acp.teardown.reap", reap_spy)
    monkeypatch.setattr("rutherford.acp.client._TERMINAL_SNAPSHOT_TIMEOUT_S", 0.05)

    await asyncio.wait_for(terminal.kill(), timeout=10.0)
    assert reaped == []  # nothing yet -- the snapshot has not returned

    release.set()
    await asyncio.wait_for(asyncio.to_thread(reap_called.wait, 5.0), timeout=10.0)

    assert reaped == [[626262]]  # the late tree was handed off and reaped


def test_both_pre_kill_snapshots_use_the_shared_helper() -> None:
    """A cheap tripwire for the most likely regression: someone re-inlining the raw snapshot call.

    Deliberately narrow, and worth being honest about what it does NOT do. It matches bare-name calls
    only, so an attribute call, an alias, or a thin wrapper walks straight past it, and it says nothing
    about whether either path stays bounded or still late-reaps -- the behavioural tests above are what
    prove that. This catches the copy-paste, not a determined divergence.
    """
    import ast

    from rutherford.acp import client as client_mod
    from rutherford.acp import session as session_mod

    for module in (client_mod, session_mod):
        module_file = module.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "snapshot_within_deadline" in called, f"{module.__name__} no longer uses the shared helper"
        assert "snapshot_descendants_eagerly" not in called, (
            f"{module.__name__} calls the raw snapshot directly again -- it must go through the bounded helper"
        )


async def test_a_saturated_executor_does_not_drop_the_terminal_reap(monkeypatch: Any) -> None:
    """A deadline must bound the WAIT, not cancel the WORK.

    `asyncio.wait_for` cancels its inner task on timeout. A `to_thread` still QUEUED on a saturated
    executor has not started, so that cancel succeeds and the reap never runs at all -- silently dropping
    a tree that was already captured. Saturation is the exact condition the deadline exists for, so
    cancelling there turns "slow" into "never".
    """
    broker = TerminalBroker(REPO_ROOT)
    term_id = await broker.create(sys.executable, ["-c", "import time; time.sleep(30)"], None)
    terminal = broker._terminals[term_id]

    reaped = threading.Event()
    monkeypatch.setattr("rutherford.acp.teardown.snapshot_descendants_eagerly", lambda pid: _immediate([737373]))
    # * client.py does `from .teardown import reap`, so the NORMAL reap resolves through client's own
    # namespace -- patching teardown.reap only reaches the late-reap path.
    monkeypatch.setattr("rutherford.acp.client.reap", lambda procs, **_: reaped.set())
    # * Deadline shorter than the executor stays blocked, so the reap is still queued when it expires.
    monkeypatch.setattr("rutherford.acp.client._TERMINAL_REAP_TIMEOUT_S", 0.05)

    release = threading.Event()
    with _single_worker_executor() as hold:
        hold(release)
        await asyncio.wait_for(terminal.kill(), timeout=10.0)
        assert not reaped.is_set()  # still queued behind the hog -- the wait timed out, as designed
        release.set()  # free the worker
        assert await asyncio.to_thread(reaped.wait, 10.0), (
            "the reap was cancelled instead of merely un-awaited: a captured tree was dropped"
        )


def _immediate(value: list[int]) -> Any:
    """A coroutine returning ``value`` now, for stubbing an async snapshot without an await point."""

    async def _run() -> list[int]:
        return value

    return _run()


@contextlib.contextmanager
def _single_worker_executor() -> Iterator[Callable[[threading.Event], None]]:
    """Make executor saturation a fact rather than a race, for any test that needs the queue to back up.

    The real default executor is ``min(32, cpu + 4)`` wide, so "submit enough hogs" is machine-dependent --
    and a test for a saturation bug must not itself depend on how busy the machine is. One worker makes
    queue order deterministic.

    The yielded ``hold`` occupies that worker and does not return until it is provably running, so a caller
    never has to sleep and hope. Reading ``_default_executor`` is private, but asyncio offers a setter with
    no getter and rejects ``set_default_executor(None)`` -- which is exactly what a loop reports before it
    has lazily built one. Centralized here, with a guard, so a future asyncio fails loudly in one place
    instead of silently mis-restoring in several.
    """
    loop = asyncio.get_running_loop()
    assert hasattr(loop, "_default_executor"), "asyncio moved the default executor; update this helper"
    previous = loop._default_executor
    narrow = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saturation-probe")
    loop.set_default_executor(narrow)
    held: list[threading.Event] = []

    def hold(release: threading.Event) -> None:
        running = threading.Event()

        def occupy() -> None:
            running.set()
            release.wait(20)

        held.append(release)
        loop.run_in_executor(None, occupy)
        assert running.wait(10), "the hog never started; the executor is not actually saturated"

    try:
        yield hold
    finally:
        for release in held:
            release.set()
        if previous is not None:
            loop.set_default_executor(previous)
        else:
            loop._default_executor = None
        narrow.shutdown(wait=False)
