# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``delegate`` tool: hand one task to one CLI."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.enums import DelegationMode
from ..domain.models import DelegationRequest, Target
from .common import parse_mode, parse_safety_mode


async def delegate_tool(
    app: AppContext,
    *,
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str = "read_only",
    mode: str = "sync",
    timeout_s: float | None = None,
    session_id: str | None = None,
    include_raw: bool = False,
    trust_workspace: bool = False,
) -> str:
    """Delegate ``prompt`` to ``(cli, model)`` and return the normalized result.

    With ``mode="async"`` the call returns a job id immediately; poll ``job_status`` /
    ``job_result``. A delegation that fails operationally (missing binary, timeout, non-zero exit)
    returns a result with ``ok=false`` and an error code, not an exception.
    """
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=prompt,
        working_dir=working_dir,
        files=files or [],
        role=role,
        safety_mode=parse_safety_mode(safety_mode),
        mode=parse_mode(mode),
        timeout_s=timeout_s,
        session_id=session_id,
        include_raw=include_raw,
        trust_workspace=trust_workspace,
        depth=app.base_depth,
    )
    correlation_id = app.new_correlation_id()

    if request.mode is DelegationMode.ASYNC:
        job = app.jobs.submit(
            "delegate",
            lambda progress: app.delegation.delegate(
                request,
                correlation_id=correlation_id,
                base_depth=app.base_depth,
                on_progress=progress,
            ),
        )
        return tool_success({"job_id": job.id, "status": job.status, "kind": job.kind})

    result = await app.delegation.delegate(request, correlation_id=correlation_id, base_depth=app.base_depth)
    return tool_success(result)
