# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``consensus`` tool: ask the same prompt of several ACP agents in parallel and return every voice."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import ConsensusRequest
from .common import apply_role, as_target, ensure_known_targets, resolve_run_mode, resolve_safety_mode
from .jobs import make_summary, submit_job


async def consensus_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    role: str | None = None,
    mode: str = "sync",
) -> str:
    """Validate the panel, fan the prompt out across the targets, and return the TOON-encoded voices.

    ``mode="async"`` submits the panel as a background job and returns a ``job_id`` immediately;
    ``mode="sync"`` (the default) awaits and returns every voice. Target/safety/mode/role validation
    always runs synchronously, so a bad panel fails on the request path rather than inside a job. A
    named ``role`` has its persona prepended to the prompt every voice receives; ``UNKNOWN_ROLE`` if
    the id is not a known role.
    """
    parsed = [as_target(target) for target in (targets or [])]
    ensure_known_targets(app.descriptors, parsed)
    safety = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    run_async = resolve_run_mode(mode)
    composed_prompt = apply_role(app.roles, role, prompt)
    request = ConsensusRequest(
        targets=parsed,
        prompt=composed_prompt,
        working_dir=working_dir,
        files=list(files) if files else [],
        role=role,
        safety_mode=safety,
        timeout_s=timeout_s,
    )

    async def run() -> str:
        result = await app.consensus.consensus(request)
        return tool_success(result)

    if run_async:
        roster = ", ".join(target.display_label for target in parsed)
        return await submit_job(app, "consensus", run, summary=make_summary("consensus", target=roster, prompt=prompt))
    return await run()
