# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``debate`` tool: several ACP agents argue a question across rounds, each on a persistent session."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import DebateRequest
from ..services.delegation import ActivityCallback
from .common import (
    apply_role,
    as_target,
    ensure_known_targets,
    parse_effort,
    parse_on_budget,
    resolve_run_mode,
    resolve_safety_mode,
)
from .jobs import make_summary, submit_job
from .panels import panel_for_call


async def debate_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    rounds: int = 2,
    judge: Any | None = None,
    working_dir: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool = True,
    timeout_s: float | None = None,
    role: str | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    persist: bool | None = None,
    external_tracking: bool = False,
    mode: str = "sync",
    on_activity: ActivityCallback | None = None,
) -> str:
    """Validate the panel, run the multi-round debate over persistent sessions, and return the transcript.

    ``targets`` is a list of ``{cli, model}`` objects (or ``cli`` / ``cli:model`` strings); a debate needs at
    least two. Alternatively name a saved ``panel`` (with optional ``panel_overrides``) instead of
    ``targets``; the two are mutually exclusive and the panel supplies the seats (``rounds`` / ``judge`` stay
    call arguments). ``mode="async"`` submits the debate as a background job and returns a ``job_id``
    immediately; ``mode="sync"`` (the default) awaits and returns the full transcript. Target/judge/safety/mode/role/
    effort/on_budget validation always runs synchronously, so a bad panel fails on the request path rather
    than in a job. A named ``role`` has its persona prepended to the opening prompt every voice argues from;
    ``UNKNOWN_ROLE`` if the id is not a known role. ``effort`` (low|medium|high|xhigh) asks every voice to
    spend more reasoning where it has a knob. ``time_budget_s`` is a wall-clock deadline for the WHOLE debate
    enforced at round boundaries: a round still in flight at the deadline is cut, the transcript so far is
    finalized (``stop_reason="budget"``), and ``on_budget`` (harvest|continue|resume, default
    ``default_on_budget``) chooses the behavior -- ``continue`` runs every round to completion. ``persist``
    keeps the debate as a durable job (F2): a parent ``state.toon`` plus the full ``transcript.md``; ``None``
    follows ``default_persistence``, ``True`` / ``False`` force it.
    """
    if panel is not None:
        # A saved panel supplies the seats (each carrying its own stance); ``rounds`` / ``judge`` stay call
        # args. ``stances`` is not a debate-tool param, so only ``targets`` is the mutual-exclusion guard.
        parsed = panel_for_call(app, panel, panel_overrides, targets, None).to_targets()
    else:
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
        effort=parse_effort(effort),
        time_budget_s=time_budget_s,
        on_budget=parse_on_budget(on_budget),
        persist=persist,
    )

    async def run(job_activity: ActivityCallback | None = None) -> str:
        # N1 (item 3): the async path hands the debate the JOB's activity sink; the sync path uses
        # ``on_activity`` (the MCP progress push). Exactly one is ever set.
        result = await app.debate.debate(request, on_activity=job_activity or on_activity)
        # Advisory F2 nudge (suppressed by external_tracking): a debate is a multi-voice run worth keeping as a
        # durable job, plus the one-time first-run setup hint.
        result.notice = app.persistence_notice(
            persisted=result.run_dir is not None, complex_run=True, external_tracking=external_tracking
        )
        return tool_success(result)

    if run_async:
        roster = ", ".join(target.display_label for target in parsed)
        return await submit_job(app, "debate", run, summary=make_summary("debate", target=roster, prompt=prompt))
    return await run()
