# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The in-memory background-job store: run long ACP work off the request path (item 9).

A sync tool awaits its service and returns the envelope; an async tool hands the same coroutine to this
store, gets a ``job_id`` back immediately, and the work runs under an :func:`asyncio.create_task`. The
store is the single owner of a job's lifecycle: it transitions ``pending`` -> ``running`` -> a terminal
state, captures the encoded result envelope (or a structured error) so it can be served later, and never
lets a background task crash the server -- any exception the work raises is folded into the job's error.

In-memory and process-global: jobs do not survive a restart (durable, replayable runs are the separate F2
:class:`~rutherford.domain.models.RunRecord` corpus). Two bounds keep the store from growing without
limit -- a ``max_jobs`` cap (evict the oldest finished job to make room, else refuse with
``TOO_MANY_JOBS``) and a ``job_ttl_s`` retention window (finished jobs older than the window are evicted
on access). The coroutine a tool submits must produce the SAME encoded envelope its sync path returns, so
async and sync results are byte-for-byte identical.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..domain.enums import JobStatus
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ActivityEvent
from .delegation import ActivityCallback

#: A factory that builds the coroutine to run, given the job's live-activity sink (N1, item 3). A factory
#: (not a bare coroutine) so the coroutine is created inside the background task, never awaited on the
#: caller's path; it is handed the per-job ``on_activity`` sink so a panel's structured stream lands in this
#: job's :class:`JobRecord.activity` buffer (the per-voice ``activity`` poll table).
CoroFactory = Callable[[ActivityCallback], Awaitable[str]]

#: The wall-clock source, injectable for tests (default :func:`time.time`).
Clock = Callable[[], float]

#: The job statuses that are finished (terminal): eligible for TTL eviction and cap-eviction.
_TERMINAL: frozenset[JobStatus] = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})

#: Cap the per-job structured activity buffer so a long debate cannot grow it without bound; the activity
#: tool only needs the latest per-voice state, comfortably within this recent-window cap (N1, item 3, 3-K).
_MAX_ACTIVITY_EVENTS = 500


@dataclass(slots=True)
class JobError:
    """The error a failed job carries: a stable code plus a human message."""

    code: ErrorCode
    message: str


@dataclass(slots=True)
class JobRecord:
    """One background job's mutable state, owned entirely by the :class:`JobStore`.

    ``result`` holds the encoded envelope STRING (the same payload the sync tool returns) once the job
    succeeds, so a later ``job_result`` serves it verbatim; ``error`` carries the structured failure when
    it fails. ``summary`` is a short, cheap one-line label (tool + a target/prompt snippet) so a listing
    is readable without the heavy result. Timestamps are wall-clock seconds from the store's clock.
    """

    job_id: str
    tool: str
    summary: str
    status: JobStatus = JobStatus.PENDING
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    result: str | None = None
    error: JobError | None = None
    #: The structured live-activity buffer (N1, item 3, decision 3-K): the same :class:`ActivityEvent`
    #: stream the sync path pushes, captured here so the ``activity`` tool can render the per-voice in-flight
    #: table (cli/model/role/status/observed/budget). Bounded to a recent window so a long run cannot grow it.
    activity: list[ActivityEvent] = field(default_factory=list)
    #: The running asyncio task, so the store can cancel it. Never serialized.
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def is_finished(self) -> bool:
        """Whether the job has reached a terminal state (succeeded / failed / cancelled)."""
        return self.status in _TERMINAL


class JobStore:
    """An async-safe, in-memory store of background jobs (item 9).

    One per :class:`~rutherford.context.AppContext`, built from config (``max_jobs`` / ``job_ttl_s``).
    All mutation goes through an :class:`asyncio.Lock`, so concurrent submits and the background tasks'
    own status transitions never race on the dict.
    """

    def __init__(self, *, max_jobs: int = 100, job_ttl_s: float = 3600.0, clock: Clock = time.time) -> None:
        self._max_jobs = max_jobs
        self._job_ttl_s = job_ttl_s
        self._clock = clock
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def submit(self, tool: str, coro_factory: CoroFactory, *, summary: str = "") -> str:
        """Create a job for ``tool``, schedule ``coro_factory`` to run in the background, and return its id.

        Returns immediately -- the work runs under an :func:`asyncio.create_task`. Evicts expired and (if
        at the cap) the oldest finished job first; raises ``TOO_MANY_JOBS`` when the cap is full of jobs
        that are still running. The background task captures any exception into the job's error, so a
        failing coroutine can never crash the server.
        """
        async with self._lock:
            self._evict_expired_locked()
            self._make_room_locked()
            job_id = uuid.uuid4().hex[:12]
            now = self._clock()
            record = JobRecord(job_id=job_id, tool=tool, summary=summary or tool, created_at=now)
            self._jobs[job_id] = record
            record.task = asyncio.create_task(self._run(record, coro_factory))
        return job_id

    def now(self) -> float:
        """The store's current wall-clock time, from its (injectable) clock.

        Exposed so a reader computing a live elapsed -- the ``activity`` snapshot -- measures against the
        same clock that stamped ``started_at`` / ``created_at``, rather than a second, possibly-skewed one.
        """
        return self._clock()

    async def get(self, job_id: str) -> JobRecord:
        """Return the job ``job_id``, evicting expired jobs first; raise ``JOB_NOT_FOUND`` if unknown."""
        async with self._lock:
            self._evict_expired_locked()
            record = self._jobs.get(job_id)
            if record is None:
                raise RutherfordError(ErrorCode.JOB_NOT_FOUND, f"no job with id {job_id!r}")
            return record

    def append_activity(self, job_id: str, event: ActivityEvent) -> None:
        """Buffer a structured :class:`ActivityEvent` on the job (N1, item 3, the poll sink of decision 3-K).

        Kept to a bounded recent window so a long run cannot grow it without limit; the ``activity`` tool reads
        only the latest per-voice state from it. Lock-free by design -- a list ``append`` and a bounded trim
        are atomic enough for a single event-loop writer, and taking the store lock per event would serialize
        every voice's emission against the store's own bookkeeping. A no-op for an unknown id.
        """
        record = self._jobs.get(job_id)
        if record is None:
            return
        record.activity.append(event)
        if len(record.activity) > _MAX_ACTIVITY_EVENTS:
            del record.activity[: len(record.activity) - _MAX_ACTIVITY_EVENTS]

    async def list(self) -> list[JobRecord]:
        """Return every retained job, newest first, evicting expired jobs first."""
        async with self._lock:
            self._evict_expired_locked()
            return sorted(self._jobs.values(), key=lambda record: record.created_at, reverse=True)

    async def cancel(self, job_id: str) -> JobRecord:
        """Cancel job ``job_id``: cancel its task and mark it ``cancelled``; raise ``JOB_NOT_FOUND`` if unknown.

        A finished job is returned unchanged (cancelling a completed job is a no-op, not an error). The
        terminal ``cancelled`` status is written by the background task's cancellation handler when the
        task was still in flight, so a just-cancelled record may still read ``running`` until the task
        unwinds; the status is forced here so the caller sees ``cancelled`` synchronously.
        """
        async with self._lock:
            self._evict_expired_locked()
            record = self._jobs.get(job_id)
            if record is None:
                raise RutherfordError(ErrorCode.JOB_NOT_FOUND, f"no job with id {job_id!r}")
            if record.is_finished:
                return record
            if record.task is not None:
                record.task.cancel()
            record.status = JobStatus.CANCELLED
            record.finished_at = self._clock()
            return record

    async def _run(self, record: JobRecord, coro_factory: CoroFactory) -> None:
        """Run one job's coroutine to completion, recording its result or error. Never raises out.

        Transitions ``pending`` -> ``running``, awaits the factory's coroutine, and stores the encoded
        envelope on success. A :class:`RutherfordError` becomes a structured job error; any other
        exception becomes an ``INTERNAL`` job error -- the background task swallows everything so an
        exception can never escape onto the event loop and bring the server down. A cancellation
        (from :meth:`cancel`) is left as the ``cancelled`` status the canceller already set.
        """
        async with self._lock:
            record.status = JobStatus.RUNNING
            record.started_at = self._clock()

        def on_activity(event: ActivityEvent) -> None:
            # N1 (decision 3-K): the structured stream feeds THIS job's poll buffer, so the ``activity`` tool
            # can read the per-voice in-flight table while the job runs.
            self.append_activity(record.job_id, event)

        try:
            result = await coro_factory(on_activity)
        except asyncio.CancelledError:
            async with self._lock:
                record.status = JobStatus.CANCELLED
                if record.finished_at is None:
                    record.finished_at = self._clock()
            raise
        except RutherfordError as exc:
            await self._finish_error(record, JobError(code=exc.code, message=exc.message))
        except Exception as exc:  # a background task must never crash the server -- capture everything
            await self._finish_error(record, JobError(code=ErrorCode.INTERNAL, message=str(exc) or "internal error"))
        else:
            async with self._lock:
                record.status = JobStatus.SUCCEEDED
                record.result = result
                record.finished_at = self._clock()

    async def _finish_error(self, record: JobRecord, error: JobError) -> None:
        """Mark ``record`` failed with ``error`` under the lock."""
        async with self._lock:
            record.status = JobStatus.FAILED
            record.error = error
            record.finished_at = self._clock()

    def _evict_expired_locked(self) -> None:
        """Drop finished jobs older than ``job_ttl_s``. The caller must hold the lock."""
        cutoff = self._clock() - self._job_ttl_s
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.is_finished and (record.finished_at or record.created_at) < cutoff
        ]
        for job_id in expired:
            del self._jobs[job_id]

    def _make_room_locked(self) -> None:
        """Ensure there is room for one more job: evict the oldest finished, else raise ``TOO_MANY_JOBS``.

        The caller must hold the lock. Eviction is oldest-finished-first (by ``created_at``); when the
        store is full of jobs that are all still running, there is nothing safe to drop, so the new
        submission is refused.
        """
        if len(self._jobs) < self._max_jobs:
            return
        finished = sorted(
            (record for record in self._jobs.values() if record.is_finished),
            key=lambda record: record.created_at,
        )
        if not finished:
            raise RutherfordError(
                ErrorCode.TOO_MANY_JOBS,
                f"the background-job cap of {self._max_jobs} is reached and every job is still running",
            )
        del self._jobs[finished[0].job_id]
