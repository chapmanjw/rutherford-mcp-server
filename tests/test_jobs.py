# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the background job store and service."""

from __future__ import annotations

import asyncio

import pytest

from rutherford.domain.enums import JobStatus
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationResult, ErrorInfo, Job, Target
from rutherford.services.jobs import JobService, JobStore


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
    store.fail(job.id, ErrorInfo(code="INTERNAL", message="boom"))
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
