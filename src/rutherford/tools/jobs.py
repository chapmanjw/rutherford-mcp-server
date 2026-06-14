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

from ..context import AppContext, tool_error, tool_success
from ..domain.enums import JobStatus
from ..domain.error_codes import ErrorCode
from ..services.jobs import CoroFactory, JobRecord

#: How much of a prompt to keep in a job's one-line summary.
_SNIPPET_LEN = 60


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
