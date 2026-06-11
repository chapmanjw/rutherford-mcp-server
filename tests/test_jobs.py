# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the background job store and service."""

from __future__ import annotations

import asyncio

import pytest
from toon import decode

from rutherford.domain.enums import JobStatus
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationResult, ErrorInfo, Job, Target
from rutherford.services.jobs import JobService, JobStore
from rutherford.tools.jobs import cancel_job_tool, job_result_tool, list_jobs_tool
from tests.fakes import make_app


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _result(text: str = "done") -> DelegationResult:
    return DelegationResult(target=Target(cli="x"), ok=True, text=text)


def test_store_lifecycle() -> None:
    store = JobStore()
    job = store.create("delegate")
    assert job.status is JobStatus.PENDING
    store.mark_running(job.id)
    assert store.get(job.id).status is JobStatus.RUNNING
    store.append_progress(job.id, "step one")
    assert store.get(job.id).progress == ["step one"]
    store.complete(job.id, _result())
    finished = store.get(job.id)
    assert finished.status is JobStatus.SUCCEEDED
    assert isinstance(finished.result, DelegationResult)
    assert finished.result.text == "done"


def test_store_fail() -> None:
    store = JobStore()
    job = store.create("delegate")
    store.fail(job.id, ErrorInfo(code=ErrorCode.INTERNAL, message="boom"))
    assert store.get(job.id).status is JobStatus.FAILED


def test_store_unknown_raises() -> None:
    with pytest.raises(RutherfordError, match="unknown job"):
        JobStore().get("missing")


def test_store_ttl_eviction() -> None:
    clock = _Clock()
    store = JobStore(ttl_s=10.0, clock=clock)
    job = store.create("delegate")
    store.complete(job.id, _result())
    clock.now = 100.0
    with pytest.raises(RutherfordError, match="unknown job"):
        store.get(job.id)


async def _wait_terminal(service: JobService, job_id: str, tries: int = 500) -> Job:
    for _ in range(tries):
        job = service.get(job_id)
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            return job
        await asyncio.sleep(0)
    raise AssertionError("job did not finish in time")


async def test_service_runs_body_to_completion() -> None:
    service = JobService()

    async def body(progress: object) -> DelegationResult:
        progress("working")  # type: ignore[operator]
        return _result("background")

    job = service.submit("delegate", body)
    final = await _wait_terminal(service, job.id)
    assert final.status is JobStatus.SUCCEEDED
    assert isinstance(final.result, DelegationResult)
    assert final.result.text == "background"
    assert "working" in final.progress


async def test_service_failing_body_marks_failed() -> None:
    service = JobService()

    async def body(progress: object) -> DelegationResult:
        raise ValueError("kaboom")

    job = service.submit("delegate", body)
    final = await _wait_terminal(service, job.id)
    assert final.status is JobStatus.FAILED
    assert final.error is not None
    assert "kaboom" in final.error.message


async def test_a_rutherford_error_from_a_body_keeps_its_code() -> None:
    # Regression (F8): a RutherfordError raised inside an async job body used to be flattened to
    # INTERNAL, dropping its code/details, while the sync tool path preserved them.
    service = JobService()

    async def body(progress: object) -> DelegationResult:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "bad arguments", details={"field": "prompt"})

    job = service.submit("delegate", body)
    final = await _wait_terminal(service, job.id)
    assert final.status is JobStatus.FAILED
    assert final.error is not None
    assert final.error.code == ErrorCode.INVALID_INPUT
    assert "bad arguments" in final.error.message
    assert final.error.details == {"field": "prompt"}


def test_max_jobs_cap_raises_too_many_jobs() -> None:
    store = JobStore(max_jobs=2)
    store.create("a")
    store.create("b")
    with pytest.raises(RutherfordError) as info:
        store.create("c")
    assert info.value.code == "TOO_MANY_JOBS"


def test_eviction_frees_a_slot_before_the_cap_bites() -> None:
    clock = _Clock()
    store = JobStore(ttl_s=10.0, max_jobs=1, clock=clock)
    job = store.create("a")
    store.complete(job.id, _result())
    clock.now = 100.0  # the completed job is now expired
    store.create("b")  # evicts the expired job first, so it does not hit the cap


def test_store_cancel_marks_cancelled_and_leaves_terminal_jobs_unchanged() -> None:
    store = JobStore()
    job = store.create("a")
    assert store.cancel(job.id).status is JobStatus.CANCELLED
    assert store.cancel(job.id).status is JobStatus.CANCELLED  # already terminal: unchanged
    done = store.create("b")
    store.complete(done.id, _result())
    assert store.cancel(done.id).status is JobStatus.SUCCEEDED  # a succeeded job is not flipped


def test_store_cancel_unknown_raises() -> None:
    with pytest.raises(RutherfordError) as info:
        JobStore().cancel("missing")
    assert info.value.code == "JOB_NOT_FOUND"


def test_a_cancelled_job_is_not_overwritten_by_a_late_completion_or_failure() -> None:
    # If a body swallows CancelledError and returns (or later raises), complete()/fail() must not
    # flip the already-CANCELLED job back to SUCCEEDED/FAILED.
    store = JobStore()
    job = store.create("a")
    store.cancel(job.id)
    store.complete(job.id, _result())
    assert store.get(job.id).status is JobStatus.CANCELLED
    store.fail(job.id, ErrorInfo(code=ErrorCode.INTERNAL, message="late"))
    assert store.get(job.id).status is JobStatus.CANCELLED
    store.mark_running(job.id)
    assert store.get(job.id).status is JobStatus.CANCELLED


def test_list_jobs_is_newest_first_and_excludes_evicted() -> None:
    clock = _Clock()
    store = JobStore(ttl_s=10.0, clock=clock)
    a = store.create("a")
    clock.now = 1.0
    b = store.create("b")
    assert [job.id for job in store.list_jobs()] == [b.id, a.id]  # newest first
    store.complete(a.id, _result())
    clock.now = 100.0
    assert [job.id for job in store.list_jobs()] == [b.id]  # the expired terminal job is gone


async def test_service_cancels_a_running_job() -> None:
    service = JobService()
    started = asyncio.Event()
    release = asyncio.Event()

    async def body(progress: object) -> DelegationResult:
        started.set()
        await release.wait()  # block until cancelled
        return _result()

    job = service.submit("delegate", body)
    await started.wait()
    assert service.cancel(job.id).status is JobStatus.CANCELLED
    await asyncio.sleep(0)  # let the cancellation propagate
    assert service.get(job.id).status is JobStatus.CANCELLED


async def test_job_tools_list_and_cancel() -> None:
    app = make_app()
    release = asyncio.Event()

    async def body(progress: object) -> DelegationResult:
        await release.wait()
        return _result()

    job = app.jobs.submit("delegate", body)
    listed = decode(await list_jobs_tool(app))
    assert any(entry["id"] == job.id for entry in listed["jobs"])
    cancelled = decode(await cancel_job_tool(app, job_id=job.id))
    assert cancelled["status"] == "cancelled"
    release.set()


async def test_job_result_on_a_running_job_is_a_still_running_notice() -> None:
    app = make_app()
    release = asyncio.Event()

    async def body(progress: object) -> DelegationResult:
        await release.wait()
        return _result()

    job = app.jobs.submit("delegate", body)
    out = decode(await job_result_tool(app, job_id=job.id))
    assert out["status"] in ("pending", "running")
    assert out["message"] == "job is still running"
    release.set()


async def test_job_result_on_a_failed_job_returns_the_error() -> None:
    app = make_app()

    async def body(progress: object) -> DelegationResult:
        raise ValueError("kaboom")

    job = app.jobs.submit("delegate", body)
    await _wait_terminal(app.jobs, job.id)
    out = decode(await job_result_tool(app, job_id=job.id))
    assert out["status"] == "failed"
    assert out["error"]["code"] == "INTERNAL"
    assert "kaboom" in out["error"]["message"]


async def test_job_result_on_a_cancelled_job_is_a_structured_notice_not_null() -> None:
    # Regression (F16): a cancelled job has result=None and error=None, so the tool used to
    # serialize the literal "null" instead of an envelope a client can act on.
    app = make_app()
    started = asyncio.Event()
    release = asyncio.Event()

    async def body(progress: object) -> DelegationResult:
        started.set()
        await release.wait()
        return _result()

    job = app.jobs.submit("delegate", body)
    await started.wait()
    app.jobs.cancel(job.id)
    await asyncio.sleep(0)  # let the cancellation propagate
    out = decode(await job_result_tool(app, job_id=job.id))
    assert out["status"] == "cancelled"
    assert out["message"] == "job was cancelled"
