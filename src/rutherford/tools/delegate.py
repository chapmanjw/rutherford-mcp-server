# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``delegate`` tool: hand a task to one ACP agent and return its normalized result."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.models import DelegationRequest, Target
from .common import ensure_known_agent, resolve_run_mode, resolve_safety_mode
from .jobs import make_summary, submit_job


async def delegate_tool(
    app: AppContext,
    *,
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    trust_workspace: bool = False,
    mode: str = "sync",
) -> str:
    """Validate the request, drive one ACP turn, and return the TOON-encoded result envelope.

    ``mode="async"`` submits the turn as a background job and returns a ``job_id`` immediately (poll it
    with ``job_status`` / ``job_result``); ``mode="sync"`` (the default) awaits and returns the result.
    Validation (known agent, safety mode, run mode) always runs synchronously, so a bad request fails on
    the request path rather than inside a job.
    """
    ensure_known_agent(app.descriptors, cli)
    safety = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    run_async = resolve_run_mode(mode)
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=prompt,
        working_dir=working_dir,
        files=list(files) if files else [],
        safety_mode=safety,
        timeout_s=timeout_s,
        trust_workspace=trust_workspace,
    )

    async def run() -> str:
        result = await app.delegation.delegate(request)
        return tool_success(result)

    if run_async:
        summary = make_summary("delegate", target=request.target.display_label, prompt=prompt)
        return await submit_job(app, "delegate", run, summary=summary)
    return await run()
