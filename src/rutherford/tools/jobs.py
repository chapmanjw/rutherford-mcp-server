# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``job_status`` and ``job_result`` tools for background executions."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.enums import JobStatus


async def job_status_tool(app: AppContext, *, job_id: str) -> str:
    """Return a background job's status and progress (raises if the id is unknown/expired)."""
    job = app.jobs.get(job_id)
    return tool_success(
        {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "progress": job.progress,
            "updated_at": job.updated_at,
        }
    )


async def job_result_tool(app: AppContext, *, job_id: str) -> str:
    """Return a finished job's result, or a still-running notice (raises if id is unknown)."""
    job = app.jobs.get(job_id)
    if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        return tool_success({"id": job.id, "status": job.status, "message": "job is still running"})
    if job.error is not None:
        return tool_success({"id": job.id, "status": job.status, "error": job.error})
    return tool_success(job.result)
