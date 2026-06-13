# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the ``activity`` tool: the structured poll view of in-flight jobs (N1, item 3)."""

from __future__ import annotations

from rutherford.domain.enums import ActivityEventKind
from rutherford.domain.models import ActivityEvent, DelegationResult, Target
from rutherford.io.serialize import decode
from rutherford.tools.activity import activity_tool
from tests.fakes import make_app


async def test_activity_is_empty_when_nothing_is_running() -> None:
    out = decode(await activity_tool(make_app()))
    assert out["running_jobs"] == 0
    assert out["activity"] == []


async def test_activity_renders_per_voice_rows_from_the_structured_buffer() -> None:
    # 3-H: one row per voice across running jobs, with the structured columns (cli/model/role/status/
    # observed), read from the buffered ActivityEvent stream (3-K) -- not from the progress strings.
    app = make_app()
    store = app.jobs.store
    job = store.create("consensus")
    store.mark_running(job.id)
    store.append_activity(job.id, ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool="consensus", declared=2))
    store.append_activity(
        job.id, ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, cli="a", model="m1", status="started")
    )
    store.append_activity(
        job.id, ActivityEvent(kind=ActivityEventKind.VOICE_FINISHED, cli="b", status="ok", observed_agents=3)
    )
    out = decode(await activity_tool(app))
    assert out["running_jobs"] == 1
    by_cli = {row["cli"]: row for row in out["activity"]}
    assert set(by_cli) == {"a", "b"}  # the panel-level PANEL_STARTED (no cli) is not a voice row
    assert by_cli["a"]["status"] == "started" and by_cli["a"]["model"] == "m1"
    assert by_cli["b"]["status"] == "ok" and by_cli["b"]["observed_agents"] == 3
    assert all(row["tool"] == "consensus" and row["job_id"] == job.id for row in out["activity"])


async def test_activity_collapses_a_voice_to_its_latest_state() -> None:
    # voice_started then a budget cut for the SAME voice -> one row, status=cut (the latest state).
    app = make_app()
    store = app.jobs.store
    job = store.create("consensus")
    store.mark_running(job.id)
    store.append_activity(
        job.id, ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, cli="a", model="m", role="r", status="started")
    )
    store.append_activity(job.id, ActivityEvent(kind=ActivityEventKind.CUT, cli="a", model="m", role="r", status="cut"))
    rows = decode(await activity_tool(app))["activity"]
    assert len(rows) == 1
    assert rows[0]["status"] == "cut"


async def test_activity_collapses_a_voice_across_a_model_fallback() -> None:
    # A model fallback rewrites model between started and finished; the STABLE correlation_id key keeps the
    # voice to ONE row (the resolved model + terminal status), not a stale "started" row plus the terminal.
    app = make_app()
    store = app.jobs.store
    job = store.create("delegate")
    store.mark_running(job.id)
    store.append_activity(
        job.id,
        ActivityEvent(
            kind=ActivityEventKind.VOICE_STARTED, correlation_id="c1", cli="x", model="requested", status="started"
        ),
    )
    store.append_activity(
        job.id,
        ActivityEvent(
            kind=ActivityEventKind.VOICE_FINISHED, correlation_id="c1", cli="x", model="resolved", status="ok"
        ),
    )
    rows = decode(await activity_tool(app))["activity"]
    assert len(rows) == 1  # one voice, one row -- not split by the model change
    assert rows[0]["status"] == "ok"
    assert rows[0]["model"] == "resolved"


async def test_activity_reports_the_latest_budget_left() -> None:
    app = make_app()
    store = app.jobs.store
    job = store.create("consensus")
    store.mark_running(job.id)
    store.append_activity(job.id, ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, cli="a", status="started"))
    store.append_activity(
        job.id, ActivityEvent(kind=ActivityEventKind.BUDGET_TICK, tool="consensus", budget_left_s=0.0)
    )
    rows = decode(await activity_tool(app))["activity"]
    assert rows[0]["budget_left_s"] == 0.0


async def test_activity_excludes_non_running_jobs() -> None:
    app = make_app()
    store = app.jobs.store
    pending = store.create("debate")  # PENDING -- must not appear
    store.append_activity(pending.id, ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, cli="x", status="started"))
    finished = store.create("delegate")
    store.mark_running(finished.id)
    store.append_activity(finished.id, ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, cli="y", status="started"))
    store.complete(finished.id, DelegationResult(target=Target(cli="y"), ok=True))
    out = decode(await activity_tool(app))
    assert out["running_jobs"] == 0
    assert out["activity"] == []
