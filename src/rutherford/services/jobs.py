# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Background jobs: start, poll, result, with a TTL'd in-memory store.

Long tasks and parallel consensus can run in the background: a tool returns a job id immediately,
and the caller polls ``job_status`` / ``job_result``. Jobs and their results live in memory with a
TTL; nothing is persisted. The :class:`JobService` schedules the work as an asyncio task and
updates the :class:`JobStore` as it progresses and completes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from ..domain.enums import JobStatus
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ConsensusResult, DebateResult, DelegationResult, ErrorInfo, Job

#: A job body: given a progress callback, produces a delegation, consensus, or debate result.
JobResult = DelegationResult | ConsensusResult | DebateResult
JobBody = Callable[[Callable[[str], None]], Awaitable[JobResult]]


class JobStore:
    """An in-memory job store with a time-to-live for completed jobs."""

    def __init__(self, ttl_s: float = 3600.0, clock: Callable[[], float] = time.time) -> None:
        self._jobs: dict[str, Job] = {}
        self._ttl_s = ttl_s
        self._clock = clock

    def create(self, kind: str) -> Job:
        """Create and store a new pending job."""
        now = self._clock()
        job = Job(id=uuid.uuid4().hex, kind=kind, status=JobStatus.PENDING, created_at=now, updated_at=now)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job:
        """Return the job, evicting expired completed jobs first, or raise if unknown."""
        self._evict_expired()
        try:
            return self._jobs[job_id]
        except KeyError:
            raise RutherfordError(ErrorCode.JOB_NOT_FOUND, f"unknown job id {job_id!r}") from None

    def mark_running(self, job_id: str) -> None:
        self._touch(job_id, JobStatus.RUNNING)

    def append_progress(self, job_id: str, line: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.progress.append(line)
            job.updated_at = self._clock()

    def complete(self, job_id: str, result: JobResult) -> None:
        """Record a finished job. The job succeeded even if its delegation result is ``ok=false``."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.result = result
            job.status = JobStatus.SUCCEEDED
            job.updated_at = self._clock()

    def fail(self, job_id: str, error: ErrorInfo) -> None:
        """Record that the job itself errored (an exception, not a delegation failure)."""
        job = self._jobs.get(job_id)
        if job is not None:
            job.error = error
            job.status = JobStatus.FAILED
            job.updated_at = self._clock()

    def _touch(self, job_id: str, status: JobStatus) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = status
            job.updated_at = self._clock()

    def _evict_expired(self) -> None:
        now = self._clock()
        terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED}
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in terminal and now - job.updated_at > self._ttl_s
        ]
        for job_id in expired:
            del self._jobs[job_id]


class JobService:
    """Schedules job bodies as asyncio tasks and tracks them in a :class:`JobStore`."""

    def __init__(self, store: JobStore | None = None) -> None:
        self._store = store or JobStore()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def store(self) -> JobStore:
        return self._store

    def submit(self, kind: str, body: JobBody) -> Job:
        """Create a job and schedule ``body`` to run in the background. Returns the new job."""
        job = self._store.create(kind)
        task = asyncio.create_task(self._run(job.id, body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job_id: str, body: JobBody) -> None:
        self._store.mark_running(job_id)
        try:
            result = await body(lambda line: self._store.append_progress(job_id, line))
            self._store.complete(job_id, result)
        except Exception as exc:  # a crashing job body becomes a failed job, not a server crash
            self._store.fail(job_id, ErrorInfo(code=str(ErrorCode.INTERNAL), message=str(exc)))

    def get(self, job_id: str) -> Job:
        """Return the current state of a job, or raise if unknown/expired."""
        return self._store.get(job_id)
