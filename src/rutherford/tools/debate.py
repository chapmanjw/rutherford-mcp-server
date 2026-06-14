# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``debate`` tool: several ACP agents argue a question across rounds, each on a persistent session."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import DebateRequest
from .common import apply_role, as_target, ensure_known_targets, resolve_run_mode, resolve_safety_mode
from .jobs import make_summary, submit_job


async def debate_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | None = None,
    rounds: int = 2,
    judge: Any | None = None,
    working_dir: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool = True,
    timeout_s: float | None = None,
    role: str | None = None,
    mode: str = "sync",
) -> str:
    """Validate the panel, run the multi-round debate over persistent sessions, and return the transcript.

    ``mode="async"`` submits the debate as a background job and returns a ``job_id`` immediately;
    ``mode="sync"`` (the default) awaits and returns the full transcript. Target/judge/safety/mode/role
    validation always runs synchronously, so a bad panel fails on the request path rather than in a job.
    A named ``role`` has its persona prepended to the opening prompt every voice argues from;
    ``UNKNOWN_ROLE`` if the id is not a known role.
    """
    parsed = [as_target(target) for target in (targets or [])]
    ensure_known_targets(app.descriptors, parsed)
    safety = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    run_async = resolve_run_mode(mode)
    judge_target = as_target(judge) if judge is not None else None
    composed_prompt = apply_role(app.roles, role, prompt)
    request = DebateRequest(
        targets=parsed,
        prompt=composed_prompt,
        rounds=rounds,
        working_dir=working_dir,
        role=role,
        safety_mode=safety,
        synthesize=synthesize,
        timeout_s=timeout_s,
        judge=judge_target,
    )

    async def run() -> str:
        result = await app.debate.debate(request)
        return tool_success(result)

    if run_async:
        roster = ", ".join(target.display_label for target in parsed)
        return await submit_job(app, "debate", run, summary=make_summary("debate", target=roster, prompt=prompt))
    return await run()
