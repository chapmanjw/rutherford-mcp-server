# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The background-job tools: submit work async, then list / inspect / fetch / cancel it (item 9).

The thin layer over :class:`~rutherford.services.jobs.JobStore`. ``submit_job`` is the async branch of
``delegate`` / ``consensus`` / ``debate``: it hands the store the SAME coroutine the sync path awaits, so
the job's eventual result envelope is byte-for-byte identical to the sync envelope, and returns a small
acknowledgement (``job_id`` + ``pending`` status). The four tool functions project a stored job into the
TOON envelopes the MCP tools return; a stored result envelope (already TOON) is served verbatim, never
re-encoded.
"""

from __future__ import annotations

from typing import TypedDict

from ..context import AppContext, tool_error, tool_success
from ..domain.enums import ActivityEventKind, JobStatus
from ..domain.error_codes import ErrorCode
from ..domain.models import ActivityEvent
from ..services.jobs import CoroFactory, JobRecord


class ActiveRow(TypedDict):
    """One in-flight VOICE's row in the ``activity`` snapshot (N1, item 3, the per-voice columns of 3-H)."""

    job_id: str
    tool: str
    cli: str | None
    model: str | None
    role: str | None
    status: str | None
    elapsed_s: float
    observed_agents: int | None
    budget_left_s: float | None


#: How much of a prompt to keep in a job's one-line summary.
_SNIPPET_LEN = 60

#: The statuses that count as "in flight" for the activity snapshot: work that is running now, plus work
#: that is queued and about to run (a submitted job is ``pending`` until its task is scheduled).
_IN_FLIGHT: frozenset[JobStatus] = frozenset({JobStatus.RUNNING, JobStatus.PENDING})

#: The activity-event kinds that describe a single voice/turn (they carry a ``cli``), as opposed to the
#: panel-level kinds (panel_started/finished, budget_tick, job_cancelled) that have no ``cli``.
_VOICE_KINDS: frozenset[ActivityEventKind] = frozenset(
    {
        ActivityEventKind.VOICE_STARTED,
        ActivityEventKind.VOICE_FINISHED,
        ActivityEventKind.CUT,
        ActivityEventKind.OBSERVED,
    }
)


def make_summary(tool: str, *, target: str | None = None, prompt: str | None = None) -> str:
    """Build a job's short one-line summary: the tool plus a target and/or a prompt snippet."""
    parts = [tool]
    if target:
        parts.append(target)
    if prompt:
        snippet = " ".join(prompt.split())
        if len(snippet) > _SNIPPET_LEN:
            snippet = snippet[: _SNIPPET_LEN - 1].rstrip() + "…"
        if snippet:
            parts.append(f"-- {snippet}")
    return " ".join(parts)


async def submit_job(app: AppContext, tool: str, coro_factory: CoroFactory, *, summary: str) -> str:
    """Submit ``coro_factory`` as a background job and return the ``{job_id, status, tool}`` envelope.

    The async branch of an orchestration tool: the coroutine runs off the request path and its result is
    fetched later with ``job_result``. The summary is precomputed by the caller (it has the resolved
    target/prompt) so the store stays orchestration-agnostic.
    """
    job_id = await app.jobs.submit(tool, coro_factory, summary=summary)
    return tool_success({"job_id": job_id, "status": JobStatus.PENDING.value, "tool": tool})


async def list_jobs_tool(app: AppContext) -> str:
    """Return every retained job as a light listing (no heavy result), newest first."""
    jobs = await app.jobs.list()
    return tool_success({"jobs": [_listing(record) for record in jobs]})


async def activity_tool(app: AppContext) -> str:
    """Return a focused snapshot of the work IN FLIGHT right now, one row PER VOICE (N1, item 3).

    The POLL half of N1's transparency (the PUSH half is MCP progress notifications on a synchronous call).
    Distinct from ``list_jobs`` (which enumerates every tracked job of every status, finished ones included):
    this is the "what is happening now" view -- across every in-flight (``running`` / ``pending``) job, one
    row per voice/turn read from the same :class:`ActivityEvent` stream a sync call pushes (decision 3-K),
    with the per-voice columns of decision 3-H: ``{job_id, tool, cli, model, role, status, elapsed_s,
    observed_agents, budget_left_s}``, sorted longest-running first. A job that has not yet emitted a voice
    event (it is still starting) contributes no rows. A synchronous call never appears here -- it has no job
    record, and its liveness rides MCP progress notifications. Returns ``{active: [], count: 0}`` when empty.
    """
    now = app.jobs.now()
    rows: list[ActiveRow] = []
    for record in await app.jobs.list():
        if record.status not in _IN_FLIGHT:
            continue
        budget_left = _latest_budget_left(record.activity)
        job_age = round(max(now - (record.started_at or record.created_at), 0.0), 3)
        for voice in _latest_voice_states(record.activity):
            rows.append(_active_row(record, voice, job_age, budget_left))
    rows.sort(key=lambda row: row["elapsed_s"], reverse=True)
    return tool_success({"active": rows, "count": len(rows)})


async def job_status_tool(app: AppContext, *, job_id: str) -> str:
    """Return one job's status and timings (no heavy result); ``JOB_NOT_FOUND`` if unknown."""
    record = await app.jobs.get(job_id)
    return tool_success(
        {
            "job_id": record.job_id,
            "tool": record.tool,
            "status": record.status.value,
            "summary": record.summary,
            "timings": {
                "created_at": record.created_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
            },
        }
    )


async def job_result_tool(app: AppContext, *, job_id: str) -> str:
    """Return a finished job's stored result envelope, or a structured error when it is not available.

    A ``succeeded`` job returns its stored envelope verbatim (already TOON-encoded by the sync path). A
    ``failed`` job returns its captured error; a ``cancelled`` job a ``cancelled`` error; a job still
    ``pending`` / ``running`` an ``INVALID_INPUT`` "not done yet" error. An unknown id raises
    ``JOB_NOT_FOUND`` through the guard.
    """
    record = await app.jobs.get(job_id)
    if record.status is JobStatus.SUCCEEDED and record.result is not None:
        return record.result
    if record.status is JobStatus.FAILED and record.error is not None:
        return tool_error(record.error.code, record.error.message, {"job_id": job_id})
    if record.status is JobStatus.CANCELLED:
        return tool_error(ErrorCode.INVALID_INPUT, f"job {job_id!r} was cancelled", {"job_id": job_id})
    return tool_error(
        ErrorCode.INVALID_INPUT,
        f"job {job_id!r} is not finished (status {record.status.value}); poll job_status and retry",
        {"job_id": job_id, "status": record.status.value},
    )


async def cancel_job_tool(app: AppContext, *, job_id: str) -> str:
    """Cancel a running job (killing its work) and return ``{job_id, status}``; ``JOB_NOT_FOUND`` if unknown."""
    record = await app.jobs.cancel(job_id)
    return tool_success({"job_id": record.job_id, "status": record.status.value})


def _listing(record: JobRecord) -> dict[str, object]:
    """Project a job into the light listing shape (no heavy result)."""
    return {
        "job_id": record.job_id,
        "tool": record.tool,
        "status": record.status.value,
        "summary": record.summary,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
    }


def _active_row(record: JobRecord, voice: ActivityEvent, job_age: float, budget_left: float | None) -> ActiveRow:
    """Project one in-flight voice into an activity row (N1, item 3, decision 3-H per-voice columns).

    A finished voice reports ITS OWN run time (carried on the terminal event); a still in-flight voice has no
    elapsed yet, so it falls back to the job's age as a live estimate. ``observed_agents`` is a floor (psutil
    sees local processes only); ``budget_left_s`` is the job's latest time-budget remaining, if any.
    """
    return {
        "job_id": record.job_id,
        "tool": record.tool,
        "cli": voice.cli,
        "model": voice.model,
        "role": voice.role,
        # ``started`` means the voice launched and is still in flight; a terminal event carries ok/failed/cut.
        "status": voice.status,
        "elapsed_s": voice.elapsed_s if voice.elapsed_s is not None else job_age,
        "observed_agents": voice.observed_agents,
        "budget_left_s": budget_left,
    }


def _latest_voice_states(events: list[ActivityEvent]) -> list[ActivityEvent]:
    """The latest event per voice, in first-seen order -- each voice's current state (N1, item 3).

    A voice emits ``voice_started`` when it launches and a terminal ``voice_finished`` / ``cut`` later;
    keeping the last event per identity collapses that to the voice's current status (so the row shows the
    resolved model and final status). The identity is the stable per-voice ``correlation_id`` -- robust to a
    model change between started and finished -- falling back to ``(cli, model, role)`` only if a producer
    left it unset. Panel-level events (no ``cli``) are skipped; they are not voices.
    """
    latest: dict[object, ActivityEvent] = {}
    for event in events:
        if event.cli is None or event.kind not in _VOICE_KINDS:
            continue
        key: object = event.correlation_id or (event.cli, event.model, event.role)
        latest[key] = event
    return list(latest.values())


def _latest_budget_left(events: list[ActivityEvent]) -> float | None:
    """The most recent time-budget remaining reported for this job (a ``budget_tick``), or ``None``."""
    budget_left: float | None = None
    for event in events:
        if event.kind is ActivityEventKind.BUDGET_TICK and event.budget_left_s is not None:
            budget_left = event.budget_left_s
    return budget_left
