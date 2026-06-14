# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``delegate`` tool: hand a task to one ACP agent and return its normalized result."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import DelegationRequest, Target
from ..services.delegation import ActivityCallback
from .common import (
    apply_role,
    as_target,
    ensure_known_agent,
    ensure_known_targets,
    parse_effort,
    resolve_run_mode,
    resolve_safety_mode,
)
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
    role: str | None = None,
    effort: str | None = None,
    fallback: list[Any] | None = None,
    allow_model_fallback: bool = True,
    mode: str = "sync",
) -> str:
    """Validate the request, drive one ACP turn (with fallback), and return the TOON-encoded result envelope.

    ``mode="async"`` submits the turn as a background job and returns a ``job_id`` immediately (poll it
    with ``job_status`` / ``job_result``); ``mode="sync"`` (the default) awaits and returns the result.
    Validation (known agent, safety mode, run mode, role, effort, fallback targets) always runs
    synchronously, so a bad request fails on the request path rather than inside a job. A named ``role`` has
    its persona prepended to ``prompt`` before the request is built; ``UNKNOWN_ROLE`` if the id is not a known
    role. ``effort`` (low|medium|high|xhigh) asks the agent to spend more reasoning where it has a knob (a
    reported no-op otherwise); omitted, the per-agent or global ``default_effort`` applies.

    ``fallback`` is an ordered list of alternate targets (``cli`` / ``cli:model`` strings, or ``{cli, model}``
    objects) to try when the primary delegation fails on a re-execution-SAFE failure (a spawn/handshake
    failure that never ran the prompt); a benched (cooled-down) alternate is skipped and the first one that
    answers becomes the result, with ``fallback_chain`` recording the failures along the way. A write/yolo
    delegation never falls back (a partial mutation may have happened). ``allow_model_fallback`` (on by
    default) lets a model-unavailable failure retry the SAME agent on its configured ``fallback_model`` first,
    where it has one (most ACP agents do not -- a clean no-op).
    """
    ensure_known_agent(app.descriptors, cli)
    safety = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    run_async = resolve_run_mode(mode)
    composed_prompt = apply_role(app.roles, role, prompt)
    fallback_targets = [as_target(target) for target in fallback] if fallback else []
    ensure_known_targets(app.descriptors, fallback_targets)
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=composed_prompt,
        working_dir=working_dir,
        files=list(files) if files else [],
        role=role,
        safety_mode=safety,
        timeout_s=timeout_s,
        trust_workspace=trust_workspace,
        effort=parse_effort(effort),
        fallback=fallback_targets,
        allow_model_fallback=allow_model_fallback,
    )

    async def run(on_activity: ActivityCallback | None = None) -> str:
        # A standalone delegation emits one voice_started/voice_finished pair (N1, item 3): on the async path
        # the job buffers them for the ``activity`` poll table; on the sync path there is no sink (None).
        result = await app.delegation.delegate(request, correlation_id="voice:0", on_activity=on_activity)
        return tool_success(result)

    if run_async:
        summary = make_summary("delegate", target=request.target.display_label, prompt=prompt)
        return await submit_job(app, "delegate", run, summary=summary)
    return await run()
