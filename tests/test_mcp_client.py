# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""End-to-end tests of the MCP layer via FastMCP's in-process client (no real CLI)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastmcp import Client

import rutherford.server as server
from rutherford.domain.models import ProcessResult
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app

EXPECTED_TOOLS = {
    "delegate",
    "consensus",
    "debate",
    "review",
    "plan",
    "capabilities",
    "doctor",
    "job_status",
    "job_result",
    "list_jobs",
    "activity",
    "cancel_job",
    "list_roles",
    "reload_panels",
    "setup",
}


@pytest.fixture
def wired_server() -> Iterator[None]:
    previous = server._APP
    server._APP = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="hello there")),
    )
    try:
        yield
    finally:
        server._APP = previous


async def test_mcp_lists_all_tools(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names == EXPECTED_TOOLS  # exact: a new/removed tool must update this set (and the docs)


async def test_sync_consensus_pushes_progress_notifications(wired_server: None) -> None:
    # N1 (item 3) push half: a synchronous panel reports progress to the caller live via MCP, the
    # voices-finished count over the declared width, so a client sees a real fraction advance.
    received: list[tuple[float, float | None, str | None]] = []

    async def handler(progress: float, total: float | None, message: str | None) -> None:
        received.append((progress, total, message))

    async with Client(server.mcp, progress_handler=handler) as client:
        await client.call_tool("consensus", {"prompt": "q", "targets": ["a", "b"]})
        await asyncio.sleep(0.1)  # let the fire-and-forget push tasks flush before the connection closes
    assert received, "expected progress notifications during a sync consensus"
    assert any(total == 2.0 for _progress, total, _message in received)  # the declared width was pushed
    assert max(progress for progress, _total, _message in received) == 2.0  # both voices finished -> 2/2


async def test_make_progress_pusher_counts_done_over_total() -> None:
    # The pusher's fraction logic in isolation: PANEL_STARTED sets the total, each VOICE_FINISHED advances
    # done, and the message rides along. Driven with a duck-typed context that records report_progress.
    from rutherford.domain.enums import ActivityEventKind
    from rutherford.domain.models import ActivityEvent

    calls: list[tuple[float, float | None, str | None]] = []

    class _RecordingContext:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            calls.append((progress, total, message))

    push = server.make_progress_pusher(_RecordingContext())  # type: ignore[arg-type]
    push(ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool="consensus", declared=2, message="started"))
    push(ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, message="a ok"))
    push(ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, message="b ok"))
    await asyncio.sleep(0.05)  # the pushes are fire-and-forget tasks; let them run
    assert calls[0] == (0.0, 2.0, "started")  # consensus maps 1:1, so a true done/total fraction
    assert calls[-1] == (2.0, 2.0, "b ok")


async def test_make_progress_pusher_leaves_debate_indeterminate() -> None:
    # A debate emits one voice_finished per TURN (turns > width, and the count varies), so it must NOT
    # report a total -- otherwise a 2-voice 2-round debate would push 4/2. Total stays None (indeterminate).
    from rutherford.domain.enums import ActivityEventKind
    from rutherford.domain.models import ActivityEvent

    calls: list[tuple[float, float | None, str | None]] = []

    class _RecordingContext:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            calls.append((progress, total, message))

    push = server.make_progress_pusher(_RecordingContext())  # type: ignore[arg-type]
    push(ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool="debate", declared=2, message="started"))
    for _ in range(4):  # 2 voices x 2 rounds
        push(ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, message="turn"))
    await asyncio.sleep(0.05)
    assert all(total is None for _progress, total, _message in calls)  # never a fraction over 100%
    assert max(progress for progress, _total, _message in calls) == 4.0  # monotonic count, no overflow


async def test_make_progress_pusher_reaches_total_when_a_voice_is_cut() -> None:
    # A budget-cut consensus voice emits CUT (not VOICE_FINISHED) but is still resolved, so the pushed
    # fraction must still reach done==total -- not stall at 1/2 -- and PANEL_FINISHED snaps to complete.
    from rutherford.domain.enums import ActivityEventKind
    from rutherford.domain.models import ActivityEvent

    calls: list[tuple[float, float | None, str | None]] = []

    class _RecordingContext:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            calls.append((progress, total, message))

    push = server.make_progress_pusher(_RecordingContext())  # type: ignore[arg-type]
    push(ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool="consensus", declared=2, message="started"))
    push(ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, message="a ok"))
    push(ActivityEvent(kind=ActivityEventKind.CUT, message="b cut"))
    push(ActivityEvent(kind=ActivityEventKind.PANEL_FINISHED, message="done"))
    await asyncio.sleep(0.05)
    assert max(progress for progress, _total, _message in calls) == 2.0  # reached 2/2 despite the cut
    assert calls[-1] == (2.0, 2.0, "done")


async def test_make_progress_pusher_counts_a_harvested_voice_once() -> None:
    # A harvested voice emits CUT then a follow-up VOICE_FINISHED reusing the SAME correlation id; the
    # pusher must count it once (keyed by id), never overshooting the declared total.
    from rutherford.domain.enums import ActivityEventKind
    from rutherford.domain.models import ActivityEvent

    calls: list[tuple[float, float | None, str | None]] = []

    class _RecordingContext:
        async def report_progress(
            self, progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            calls.append((progress, total, message))

    push = server.make_progress_pusher(_RecordingContext())  # type: ignore[arg-type]
    push(ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool="consensus", declared=2, message="started"))
    push(ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, correlation_id="c0", message="a ok"))
    push(ActivityEvent(kind=ActivityEventKind.CUT, correlation_id="c1", message="b cut"))
    push(ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, correlation_id="c1", message="b harvested"))
    await asyncio.sleep(0.05)
    assert max(progress for progress, _total, _message in calls) == 2.0  # not 3.0 -- the harvest is one voice


async def test_mcp_delegate_round_trip(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("delegate", {"cli": "a", "prompt": "hi"})
        text = result.content[0].text
        assert "ok: true" in text
        assert "hello there" in text


async def test_mcp_debate_round_trip(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("debate", {"prompt": "q", "targets": ["a", "b"], "rounds": 1})
        text = result.content[0].text
        assert "rounds[1]" in text
        assert "hello there" in text


async def test_mcp_list_roles(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("list_roles", {})
        assert "planner" in result.content[0].text


async def test_mcp_tool_error_surfaces(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        with pytest.raises(Exception, match="INVALID_INPUT"):
            await client.call_tool("delegate", {"cli": "a", "prompt": "hi", "safety_mode": "bogus"})
