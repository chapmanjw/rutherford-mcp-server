# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the in-memory background-job system: the store, the job tools, and the async tool path."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import ActivityEventKind, JobStatus
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ActivityEvent
from rutherford.io.serialize import decode
from rutherford.services.delegation import ActivityCallback
from rutherford.services.jobs import JobStore
from rutherford.tools.consensus import consensus_tool
from rutherford.tools.delegate import delegate_tool
from rutherford.tools.jobs import (
    activity_tool,
    cancel_job_tool,
    job_result_tool,
    job_status_tool,
    list_jobs_tool,
    make_summary,
    submit_job,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))


def _app(config: RutherfordConfig | None = None) -> AppContext:
    return build_app_context(config=config or RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))


async def _poll_until_done(store: JobStore, job_id: str, *, timeout_s: float = 5.0) -> Any:
    """Poll a job until it leaves the running/pending states, or fail the test on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        record = await store.get(job_id)
        if record.is_finished:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


# --- JobStore lifecycle ------------------------------------------------------


async def test_submit_runs_and_stores_result() -> None:
    store = JobStore()

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return "the-envelope"

    job_id = await store.submit("delegate", work, summary="delegate fake")
    assert len(job_id) == 12
    record = await _poll_until_done(store, job_id)
    assert record.status is JobStatus.SUCCEEDED
    assert record.result == "the-envelope"
    assert record.error is None
    assert record.started_at is not None and record.finished_at is not None


async def test_async_job_serves_the_sync_envelope_verbatim() -> None:
    """A job serves its coroutine's encoded envelope byte-for-byte (no re-encoding by the job layer).

    Uses a fixed envelope so the assertion is exact: the value the sync tool would return is the value
    ``job_result`` serves. (Two real delegations would differ only in the per-run ``duration_s`` float,
    so the envelope-identity guarantee is proven against a deterministic payload, not run-to-run timing.)
    """
    app = _app()

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return await delegate_tool(app, cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))

    sync_envelope = await work()

    async def fixed(_on_activity: ActivityCallback | None = None) -> str:
        return sync_envelope

    job_id = await app.jobs.submit("delegate", fixed, summary="x")
    record = await _poll_until_done(app.jobs, job_id)
    assert record.status is JobStatus.SUCCEEDED
    assert record.result == sync_envelope  # served verbatim, not re-encoded
    assert await job_result_tool(app, job_id=job_id) == sync_envelope


async def test_async_delegation_envelope_matches_sync_shape() -> None:
    """A real async delegation yields the same envelope SHAPE as sync (only the duration float differs)."""
    app = _app()

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return await delegate_tool(app, cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))

    sync = decode(await work())
    job_id = await app.jobs.submit("delegate", work, summary="x")
    record = await _poll_until_done(app.jobs, job_id)
    assert record.status is JobStatus.SUCCEEDED
    assert record.result is not None
    asynced = decode(record.result)
    # Drop the only run-to-run-volatile field; everything else must match exactly.
    sync.pop("duration_s", None)
    asynced.pop("duration_s", None)
    assert asynced == sync
    assert "42" in record.result


async def test_failing_coro_is_captured_as_failed() -> None:
    store = JobStore()

    async def boom(_on_activity: ActivityCallback | None = None) -> str:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "bad input")

    record = await _poll_until_done(store, await store.submit("delegate", boom, summary="x"))
    assert record.status is JobStatus.FAILED
    assert record.error is not None
    assert record.error.code is ErrorCode.INVALID_INPUT
    assert record.error.message == "bad input"


async def test_unexpected_exception_becomes_internal_error() -> None:
    store = JobStore()

    async def crash(_on_activity: ActivityCallback | None = None) -> str:
        raise ValueError("kaboom")

    record = await _poll_until_done(store, await store.submit("delegate", crash, summary="x"))
    assert record.status is JobStatus.FAILED
    assert record.error is not None
    assert record.error.code is ErrorCode.INTERNAL
    assert "kaboom" in record.error.message


async def test_cancel_long_running_job() -> None:
    store = JobStore()
    started = asyncio.Event()

    async def long_work(_on_activity: ActivityCallback | None = None) -> str:
        started.set()
        await asyncio.sleep(30)
        return "never"

    job_id = await store.submit("delegate", long_work, summary="x")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    record = await store.cancel(job_id)
    assert record.status is JobStatus.CANCELLED
    assert record.finished_at is not None
    # The result is never set on a cancelled job.
    again = await store.get(job_id)
    assert again.status is JobStatus.CANCELLED and again.result is None


async def test_cancel_finished_job_is_a_noop() -> None:
    store = JobStore()

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return "done"

    job_id = await store.submit("delegate", work, summary="x")
    await _poll_until_done(store, job_id)
    record = await store.cancel(job_id)
    assert record.status is JobStatus.SUCCEEDED  # unchanged, not flipped to cancelled


async def test_get_and_cancel_unknown_id_raise_job_not_found() -> None:
    store = JobStore()
    for action in (store.get, store.cancel):
        with pytest.raises(RutherfordError) as exc:
            await action("nope")
        assert exc.value.code is ErrorCode.JOB_NOT_FOUND


async def test_list_is_newest_first() -> None:
    clock = _FakeClock()
    store = JobStore(clock=clock)

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return "ok"

    first = await store.submit("delegate", work, summary="first")
    clock.advance(1.0)
    second = await store.submit("consensus", work, summary="second")
    for job_id in (first, second):
        await _poll_until_done(store, job_id)
    jobs = await store.list()
    assert [record.job_id for record in jobs] == [second, first]


# --- TTL and cap -------------------------------------------------------------


async def test_ttl_evicts_finished_jobs_on_access() -> None:
    clock = _FakeClock()
    store = JobStore(job_ttl_s=10.0, clock=clock)

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return "ok"

    job_id = await store.submit("delegate", work, summary="x")
    await _poll_until_done(store, job_id)
    clock.advance(11.0)  # past the TTL
    with pytest.raises(RutherfordError) as exc:
        await store.get(job_id)
    assert exc.value.code is ErrorCode.JOB_NOT_FOUND


async def test_cap_evicts_oldest_finished() -> None:
    clock = _FakeClock()
    store = JobStore(max_jobs=2, clock=clock)

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return "ok"

    a = await store.submit("delegate", work, summary="a")
    await _poll_until_done(store, a)
    clock.advance(1.0)
    b = await store.submit("delegate", work, summary="b")
    await _poll_until_done(store, b)
    clock.advance(1.0)
    c = await store.submit("delegate", work, summary="c")  # evicts a (oldest finished)
    await _poll_until_done(store, c)
    ids = {record.job_id for record in await store.list()}
    assert ids == {b, c}
    with pytest.raises(RutherfordError):
        await store.get(a)


async def test_cap_refuses_when_full_of_running() -> None:
    store = JobStore(max_jobs=1)
    hold = asyncio.Event()

    async def blocked(_on_activity: ActivityCallback | None = None) -> str:
        await hold.wait()
        return "ok"

    running = await store.submit("delegate", blocked, summary="busy")
    with pytest.raises(RutherfordError) as exc:
        await store.submit("delegate", blocked, summary="overflow")
    assert exc.value.code is ErrorCode.TOO_MANY_JOBS
    hold.set()  # let the running job finish so the event loop unwinds cleanly
    await _poll_until_done(store, running)


# --- Job tool shapes ---------------------------------------------------------


async def test_job_tools_status_result_list_shapes() -> None:
    app = _app()

    async def work(_on_activity: ActivityCallback | None = None) -> str:
        return "RESULT-ENVELOPE"

    submit = decode(await submit_job(app, "delegate", work, summary="delegate fake -- hi"))
    assert submit["status"] == "pending" and submit["tool"] == "delegate"
    job_id = submit["job_id"]
    await _poll_until_done(app.jobs, job_id)

    status = decode(await job_status_tool(app, job_id=job_id))
    assert status["job_id"] == job_id
    assert status["tool"] == "delegate"
    assert status["status"] == "succeeded"
    assert status["summary"] == "delegate fake -- hi"
    assert set(status["timings"]) == {"created_at", "started_at", "finished_at"}

    # job_result returns the stored envelope verbatim.
    assert await job_result_tool(app, job_id=job_id) == "RESULT-ENVELOPE"

    listing = decode(await list_jobs_tool(app))
    row = next(job for job in listing["jobs"] if job["job_id"] == job_id)
    assert set(row) == {"job_id", "tool", "status", "summary", "created_at", "finished_at"}


async def test_job_result_for_failed_and_cancelled_and_pending() -> None:
    app = _app()

    async def boom(_on_activity: ActivityCallback | None = None) -> str:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "nope")

    failed_id = decode(await submit_job(app, "delegate", boom, summary="x"))["job_id"]
    await _poll_until_done(app.jobs, failed_id)
    failed = decode(await job_result_tool(app, job_id=failed_id))
    assert failed["error"]["code"] == "INVALID_INPUT"
    assert failed["error"]["details"]["job_id"] == failed_id

    hold = asyncio.Event()

    async def slow(_on_activity: ActivityCallback | None = None) -> str:
        await hold.wait()
        return "done"

    pending_id = decode(await submit_job(app, "delegate", slow, summary="x"))["job_id"]
    pending = decode(await job_result_tool(app, job_id=pending_id))
    assert pending["error"]["code"] == "INVALID_INPUT"
    assert pending["error"]["details"]["status"] in {"pending", "running"}

    cancelled = decode(await cancel_job_tool(app, job_id=pending_id))
    assert cancelled["status"] == "cancelled"
    cancelled_result = decode(await job_result_tool(app, job_id=pending_id))
    assert cancelled_result["error"]["code"] == "INVALID_INPUT"
    assert "cancelled" in cancelled_result["error"]["message"]
    hold.set()


async def test_job_tools_unknown_id_raises() -> None:
    app = _app()
    for action in (job_status_tool, job_result_tool, cancel_job_tool):
        with pytest.raises(RutherfordError) as exc:
            await action(app, job_id="missing")
        assert exc.value.code is ErrorCode.JOB_NOT_FOUND


def test_make_summary_truncates_long_prompts() -> None:
    short = make_summary("delegate", target="fake", prompt="hello world")
    assert short == "delegate fake -- hello world"
    long = make_summary("consensus", target="a, b", prompt="x " * 200)
    assert long.startswith("consensus a, b -- ")
    assert long.endswith("…")
    assert len(long) < 120


# --- async tool path through the services and server -------------------------


async def test_delegate_tool_async_returns_job_and_completes() -> None:
    app = _app()
    submit = decode(
        await delegate_tool(app, cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), mode="async")
    )
    assert submit["status"] == "pending" and submit["tool"] == "delegate"
    record = await _poll_until_done(app.jobs, submit["job_id"])
    assert record.status is JobStatus.SUCCEEDED
    assert record.result is not None and "42" in record.result


async def test_consensus_tool_async_returns_job() -> None:
    app = _app()
    submit = decode(
        await consensus_tool(
            app, prompt="what is 17 + 25?", targets=["fake", "fake:m"], working_dir=str(REPO_ROOT), mode="async"
        )
    )
    assert submit["tool"] == "consensus"
    record = await _poll_until_done(app.jobs, submit["job_id"])
    assert record.status is JobStatus.SUCCEEDED
    # Match the encoded answer field (`text: "42"`) so the count cannot be inflated by "42" in a duration
    # float; the async envelope is byte-identical to the sync one, so both voices answer here too.
    assert record.result is not None and record.result.count('text: "42"') == 2


async def test_delegate_tool_rejects_unknown_mode() -> None:
    app = _app()
    with pytest.raises(RutherfordError) as exc:
        await delegate_tool(app, cli="fake", prompt="x", mode="bogus")
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_server_delegate_async_and_job_tools(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", _app())
    submit = decode(
        await server.delegate(cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), mode="async")
    )
    job_id = submit["job_id"]
    assert submit["status"] == "pending"
    await _poll_until_done(server.get_app().jobs, job_id)
    result = await server.job_result(job_id=job_id)
    assert "42" in result
    status = decode(await server.job_status(job_id=job_id))
    assert status["status"] == "succeeded"
    listing = decode(await server.list_jobs())
    assert any(job["job_id"] == job_id for job in listing["jobs"])


# --- activity (in-flight snapshot) -------------------------------------------


async def test_activity_is_empty_when_idle() -> None:
    app = _app()
    snapshot = decode(await activity_tool(app))
    assert snapshot["count"] == 0
    assert snapshot["active"] == []


def _voice_event(cli: str = "fake", *, model: str | None = "m", status: str = "started") -> ActivityEvent:
    """A ``voice_started`` activity event, the per-voice row the activity tool projects (N1, item 3)."""
    return ActivityEvent(
        kind=ActivityEventKind.VOICE_STARTED, correlation_id="voice:0", cli=cli, model=model, status=status
    )


async def test_activity_shows_in_flight_voice_with_per_voice_columns() -> None:
    app = _app()
    hold = asyncio.Event()
    started = asyncio.Event()

    async def in_flight(on_activity: ActivityCallback | None = None) -> str:
        if on_activity is not None:
            on_activity(_voice_event())  # the job emits one voice row, the per-voice activity shape
        started.set()
        await hold.wait()
        return "done"

    job_id = await app.jobs.submit("consensus", in_flight, summary="consensus fake -- working")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0.01)  # let a little wall-clock pass so the live elapsed is strictly positive

    snapshot = decode(await activity_tool(app))
    assert snapshot["count"] == 1
    row = snapshot["active"][0]
    assert row["job_id"] == job_id
    assert row["tool"] == "consensus"
    assert row["cli"] == "fake"
    assert row["model"] == "m"
    assert row["status"] == "started"  # the voice launched and is still in flight
    assert row["elapsed_s"] > 0.0  # an in-flight voice falls back to the job's live age
    assert set(row) == {
        "job_id",
        "tool",
        "cli",
        "model",
        "role",
        "status",
        "elapsed_s",
        "observed_agents",
        "budget_left_s",
    }

    hold.set()  # release the job so the loop unwinds cleanly
    await _poll_until_done(app.jobs, job_id)


async def test_activity_excludes_finished_jobs() -> None:
    app = _app()

    async def quick(on_activity: ActivityCallback | None = None) -> str:
        if on_activity is not None:
            on_activity(_voice_event())
        return "done"

    finished_id = await app.jobs.submit("delegate", quick, summary="finished")
    await _poll_until_done(app.jobs, finished_id)

    hold = asyncio.Event()
    started = asyncio.Event()

    async def in_flight(on_activity: ActivityCallback | None = None) -> str:
        if on_activity is not None:
            on_activity(_voice_event())
        started.set()
        await hold.wait()
        return "done"

    running_id = await app.jobs.submit("consensus", in_flight, summary="running")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    snapshot = decode(await activity_tool(app))
    ids = {row["job_id"] for row in snapshot["active"]}
    assert running_id in ids
    assert finished_id not in ids  # a finished job never appears in the in-flight snapshot
    assert snapshot["count"] == len(snapshot["active"])

    hold.set()
    await _poll_until_done(app.jobs, running_id)


async def test_activity_sorted_longest_running_first() -> None:
    app = _app()
    hold = asyncio.Event()

    async def in_flight(on_activity: ActivityCallback | None = None) -> str:
        if on_activity is not None:
            on_activity(_voice_event())
        await hold.wait()
        return "done"

    first = await app.jobs.submit("delegate", in_flight, summary="first")
    await asyncio.sleep(0.05)  # the first job accrues more elapsed than the second
    second = await app.jobs.submit("consensus", in_flight, summary="second")
    await asyncio.sleep(0.02)

    snapshot = decode(await activity_tool(app))
    order = [row["job_id"] for row in snapshot["active"]]
    assert order == [first, second]  # longest-running first
    assert snapshot["active"][0]["elapsed_s"] >= snapshot["active"][1]["elapsed_s"]

    hold.set()
    for job_id in (first, second):
        await _poll_until_done(app.jobs, job_id)


async def test_server_activity_tool(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", _app())
    hold = asyncio.Event()
    started = asyncio.Event()

    async def in_flight(on_activity: ActivityCallback | None = None) -> str:
        if on_activity is not None:
            on_activity(_voice_event())
        started.set()
        await hold.wait()
        return "done"

    job_id = await server.get_app().jobs.submit("consensus", in_flight, summary="x")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    snapshot = decode(await server.activity())
    assert snapshot["count"] >= 1
    assert any(row["job_id"] == job_id for row in snapshot["active"])

    hold.set()
    await _poll_until_done(server.get_app().jobs, job_id)


class _FakeClock:
    """A controllable wall clock for the TTL/cap and ordering tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
