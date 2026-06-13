# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``activity`` tool: a live, structured snapshot of in-flight background work (N1, item 3)."""

from __future__ import annotations

import time
from typing import Any

from ..context import AppContext, tool_success
from ..domain.enums import ActivityEventKind, JobStatus
from ..domain.models import ActivityEvent

#: The activity-event kinds that describe a single voice/turn (they carry a ``cli``), as opposed to the
#: panel-level kinds (panel_started/finished, budget_tick, job_cancelled) that have no ``cli``.
_VOICE_KINDS = frozenset(
    {
        ActivityEventKind.VOICE_STARTED,
        ActivityEventKind.VOICE_FINISHED,
        ActivityEventKind.CUT,
        ActivityEventKind.OBSERVED,
    }
)


async def activity_tool(app: AppContext) -> str:
    """Return a structured snapshot of the in-flight work across the background jobs running right now.

    The POLL half of N1's transparency (the PUSH half is MCP progress notifications on a synchronous call):
    one row per voice/turn across every ``RUNNING`` job, read from the same :class:`ActivityEvent` stream a
    sync call pushes (decision 3-K), with the columns decision 3-H names -- job id, tool, cli, model, role,
    status, elapsed, observed agents, budget left. Distinct from ``list_jobs`` (which enumerates job
    RECORDS, terminal ones included); this is the live in-flight tree, the one to watch while panels run and
    to pick a ``job_id`` to ``cancel_job``. A synchronous call never appears here -- it has no job record,
    and its liveness rides MCP progress notifications back to the caller instead.

    Rows are a uniform array so the TOON seam renders them as a compact table.
    """
    now = time.time()
    running = [job for job in app.jobs.list_jobs() if job.status is JobStatus.RUNNING]
    rows: list[dict[str, Any]] = []
    for job in running:
        job_age = round(now - job.created_at, 1)
        budget_left = _latest_budget_left(job.activity)
        for voice in _latest_voice_states(job.activity):
            rows.append(
                {
                    "job_id": job.id,
                    "tool": job.kind,
                    "cli": voice.cli,
                    "model": voice.model,
                    "role": voice.role,
                    # ``started`` means the voice launched and is still in flight; a terminal event carries
                    # ``ok`` / ``failed`` / ``cut``.
                    "status": voice.status,
                    # A finished voice reports ITS OWN run time (carried on the terminal event); a still
                    # in-flight one has no elapsed yet, so fall back to the job's age as a live estimate.
                    "elapsed_s": voice.elapsed_s if voice.elapsed_s is not None else job_age,
                    "observed_agents": voice.observed_agents,
                    "budget_left_s": budget_left,
                }
            )
    return tool_success(
        {
            "running_jobs": len(running),
            "activity": rows,
            "note": (
                "In-flight work across running background jobs only (distinct from list_jobs, which lists "
                "every job). A synchronous call has no job record; its progress rides MCP notifications. "
                "observed_agents is a floor -- psutil sees local processes only."
            ),
        }
    )


def _latest_voice_states(events: list[ActivityEvent]) -> list[ActivityEvent]:
    """The latest event per voice, in first-seen order -- each voice's current state.

    A voice emits voice_started when it launches and a terminal voice_finished / cut later; keeping the last
    event per identity collapses that to the voice's current status (so the row shows the resolved model and
    final status). The identity is the stable per-voice ``correlation_id`` -- robust to a model fallback that
    rewrites ``model`` between started and finished -- falling back to ``(cli, model, role)`` only if some
    producer left it unset. Panel-level events (no ``cli``) are skipped; they are not voices.
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
