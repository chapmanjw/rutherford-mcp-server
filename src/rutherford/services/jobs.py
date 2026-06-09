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
from ..domain.models import ConsensusResult, DebateResult, DelegationResult, ErrorInfo, Job, StrategyResult
from ..runtime.logging import log_event

#: A job body: given a progress callback, produces a delegation, consensus, debate, or strategy result.
JobResult = DelegationResult | ConsensusResult | DebateResult | StrategyResult
JobBody = Callable[[Callable[[str], None]], Awaitable[JobResult]]

#: Job states past which nothing more happens: eligible for TTL eviction and not cancellable.
_TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class JobStore:
    """An in-memory job store with a time-to-live for terminal jobs and a creation cap."""

    def __init__(
        self,
        ttl_s: float = 3600.0,
        max_jobs: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._ttl_s = ttl_s
        self._max_jobs = max_jobs
        self._clock = clock

    def create(self, kind: str) -> Job:
        """Create and store a new pending job, enforcing the ``max_jobs`` cap.

        Expired terminal jobs are evicted first, so a finished-but-not-yet-evicted job frees a slot
        before the cap bites. A genuinely full store raises ``TOO_MANY_JOBS`` rather than growing
        unbounded.
        """
        self._evict_expired()
        if len(self._jobs) >= self._max_jobs:
            raise RutherfordError(
                ErrorCode.TOO_MANY_JOBS,
                f"too many background jobs (cap {self._max_jobs}); wait for some to finish, cancel one, "
                "or raise max_jobs",
            )
        now = self._clock()
        job = Job(id=uuid.uuid4().hex, kind=kind, status=JobStatus.PENDING, created_at=now, updated_at=now)
        self._jobs[job.id] = job
        return job

    def list_jobs(self) -> list[Job]:
        """Return all live jobs (expired terminal ones evicted), newest first."""
        self._evict_expired()
        return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def cancel(self, job_id: str) -> Job:
        """Mark a non-terminal job ``CANCELLED`` and return it; a terminal job is returned unchanged.

        Raises ``JOB_NOT_FOUND`` for an unknown id. The actual asyncio task cancellation (and the CLI
        process-tree kill) is driven by :class:`JobService`.
        """
        job = self.get(job_id)
        if job.status not in _TERMINAL_STATUSES:
            job.status = JobStatus.CANCELLED
            job.updated_at = self._clock()
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
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES and now - job.updated_at > self._ttl_s
        ]
        for job_id in expired:
            del self._jobs[job_id]


class JobService:
    """Schedules job bodies as asyncio tasks and tracks them in a :class:`JobStore`."""

    def __init__(self, store: JobStore | None = None) -> None:
        self._store = store or JobStore()
        #: Live tasks keyed by job id, so ``cancel`` can find and cancel the right one.
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def store(self) -> JobStore:
        return self._store

    def submit(self, kind: str, body: JobBody) -> Job:
        """Create a job and schedule ``body`` to run in the background. Returns the new job.

        ``create`` enforces the ``max_jobs`` cap before anything is scheduled, so a full store raises
        ``TOO_MANY_JOBS`` instead of launching an unbounded task.
        """
        job = self._store.create(kind)
        task = asyncio.create_task(self._run(job.id, body))
        self._tasks[job.id] = task
        task.add_done_callback(self._on_task_done)
        log_event("job_submitted", job_id=job.id, kind=kind)
        return job

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop a finished task from the live-task map (registered as the task's done callback)."""
        for job_id, tracked in list(self._tasks.items()):
            if tracked is task:
                del self._tasks[job_id]
                return

    async def _run(self, job_id: str, body: JobBody) -> None:
        self._store.mark_running(job_id)
        try:
            result = await body(lambda line: self._store.append_progress(job_id, line))
            self._store.complete(job_id, result)
            log_event("job_finished", job_id=job_id, status=JobStatus.SUCCEEDED.value)
        except asyncio.CancelledError:
            # Cancellation (via cancel()) records CANCELLED, not FAILED; re-raise so the task ends
            # cancelled. The underlying AsyncProcessRunner already killed the CLI process tree.
            self._store.cancel(job_id)
            raise
        except Exception as exc:  # a crashing job body becomes a failed job, not a server crash
            self._store.fail(job_id, ErrorInfo(code=str(ErrorCode.INTERNAL), message=str(exc)))
            log_event("job_finished", job_id=job_id, status=JobStatus.FAILED.value, error=str(exc))

    def cancel(self, job_id: str) -> Job:
        """Cancel a running/pending job, killing its CLI process tree; return the updated job.

        Marks the store entry ``CANCELLED`` (raising ``JOB_NOT_FOUND`` for an unknown id) and cancels
        the asyncio task; a job already in a terminal state is returned unchanged.
        """
        job = self._store.cancel(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        log_event("job_cancelled", job_id=job_id, status=job.status.value)
        return job

    def list_jobs(self) -> list[Job]:
        """Return all live jobs, newest first."""
        return self._store.list_jobs()

    def get(self, job_id: str) -> Job:
        """Return the current state of a job, or raise if unknown/expired."""
        return self._store.get(job_id)
