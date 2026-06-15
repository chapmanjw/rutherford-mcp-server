# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``consensus`` tool: ask the same prompt of several ACP agents in parallel and reduce the voices."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import ConsensusRequest, Target
from ..services.delegation import ActivityCallback
from .common import (
    apply_role,
    as_target,
    ensure_known_agent,
    ensure_known_targets,
    parse_effort,
    parse_on_budget,
    parse_stances,
    parse_strategy,
    resolve_run_mode,
    resolve_safety_mode,
)
from .jobs import make_summary, submit_job
from .panels import panel_for_call


async def consensus_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | str | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    strategy: str | None = None,
    verdict_schema: dict[str, Any] | None = None,
    judge: Any = None,
    stances: list[str] | None = None,
    expand_all: bool = False,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    persist: bool | None = None,
    external_tracking: bool = False,
    mode: str = "sync",
    on_activity: ActivityCallback | None = None,
) -> str:
    """Validate the panel, fan the prompt out across the targets, and reduce the voices to a TOON envelope.

    ``targets`` is a list of ``{cli, model}`` objects (or ``cli`` / ``cli:model`` strings). Omit it, pass
    an empty list, pass the sentinel ``"all"``, or set ``expand_all=true`` to fan out to every registered
    agent (each at its default model, capped at ``max_targets``); the result's ``skipped`` field explains
    any agent left out. Alternatively name a saved ``panel`` (with optional one-off ``panel_overrides``)
    instead of ``targets``; a panel supplies the seats and the aggregation ``strategy`` and is mutually
    exclusive with ``targets`` / ``stances``. Optional ``stances`` (parallel to ``targets``) steer each voice
    for/against/neutral and cannot be combined with the auto-expanded panel. With a ``strategy`` other than
    ``all-voices``
    (optionally with a ``verdict_schema``), the voices are aggregated into a :class:`StrategyResult`
    outcome instead of returned individually. ``synthesize`` (tri-state; defaults to ``synthesize_default``,
    off out of the box) adds a server-side combined answer (``all-voices`` only); ``judge`` names the seat
    that writes it. ``effort`` (low|medium|high|xhigh) asks every voice to spend more reasoning where it has
    a knob. ``time_budget_s`` is a wall-clock deadline for the WHOLE panel (distinct from each voice's
    ``timeout_s``): at the deadline answered voices are kept, in-flight ones are cut, and the panel
    aggregates over the harvest if ``min_quorum`` usable remain (``stop_reason="budget"``) -- fewer than
    ``min_quorum`` is ``BUDGET_EXHAUSTED``. ``on_budget`` (harvest|continue|resume, default
    ``default_on_budget``) chooses the deadline behavior. ``mode="async"`` submits the panel as a background
    job and returns a ``job_id``; ``mode="sync"`` (the default) awaits it. Validation always runs on the
    request path, so a bad panel fails there rather than inside a job. A named ``role`` has its persona
    prepended to the prompt every voice receives; ``UNKNOWN_ROLE`` if the id is not a known role. ``persist``
    keeps the panel as a durable job (F2): a parent ``state.toon`` linking a child record per voice, plus
    ``voices/voice-N.md`` artifacts; ``None`` follows ``default_persistence``, ``True`` / ``False`` force it.
    """
    parsed: list[Target]
    parsed_stances = parse_stances(stances)
    panel_strategy: str | None = None
    if panel is not None:
        # A saved panel supplies the seats (each carrying its own stance) and the aggregation strategy; it is
        # mutually exclusive with ``targets`` / ``stances`` (panel_for_call rejects either alongside it).
        resolved_panel = panel_for_call(app, panel, panel_overrides, targets, stances)
        parsed = resolved_panel.to_targets()
        panel_strategy = resolved_panel.strategy
        parsed_stances = None
        auto_panel = False
    else:
        auto_panel = expand_all or _wants_all(targets)
        if auto_panel:
            parsed = []
        elif isinstance(targets, str):
            parsed = [as_target(targets)]  # a bare "cli" / "cli:model" string
        else:
            parsed = [as_target(target) for target in (targets or [])]
    if not auto_panel:
        ensure_known_targets(app.descriptors, parsed)
    judge_target = as_target(judge) if judge is not None else None
    if judge_target is not None:
        ensure_known_agent(app.descriptors, judge_target.cli)  # a typo'd judge is a clean error, not silent
    safety = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    run_async = resolve_run_mode(mode)
    composed_prompt = apply_role(app.roles, role, prompt)
    # An explicit ``strategy`` wins; else the panel's own ``strategy``; else the legacy all-voices path.
    effective_strategy = strategy if strategy is not None else panel_strategy
    request = ConsensusRequest(
        targets=parsed,
        prompt=composed_prompt,
        stances=parsed_stances,
        working_dir=working_dir,
        files=list(files) if files else [],
        role=role,
        safety_mode=safety,
        synthesize=synthesize,
        timeout_s=timeout_s,
        expand_all=auto_panel,
        strategy=parse_strategy(effective_strategy) if effective_strategy is not None else parse_strategy("all-voices"),
        verdict_schema=verdict_schema,
        judge=judge_target,
        effort=parse_effort(effort),
        time_budget_s=time_budget_s,
        on_budget=parse_on_budget(on_budget),
        persist=persist,
    )

    async def run(job_activity: ActivityCallback | None = None) -> str:
        # N1 (item 3): the async path hands the panel the JOB's activity sink (the ``activity`` poll table);
        # the sync path uses ``on_activity`` (the MCP progress push), supplied by server.py. Exactly one is
        # ever set -- a sync call has no job buffer, an async call has no live caller to push to.
        result = await app.consensus.consensus(request, on_activity=job_activity or on_activity)
        # Advisory F2 nudge (suppressed by external_tracking): a consensus panel is a multi-voice run worth
        # keeping as a durable job, plus the one-time first-run setup hint.
        result.notice = app.persistence_notice(
            persisted=result.run_dir is not None, complex_run=True, external_tracking=external_tracking
        )
        return tool_success(result)

    if run_async:
        roster = ", ".join(target.display_label for target in parsed) or "all"
        return await submit_job(app, "consensus", run, summary=make_summary("consensus", target=roster, prompt=prompt))
    return await run()


def _wants_all(targets: list[Any] | str | None) -> bool:
    """Whether the caller asked for the full panel: targets omitted, empty, or the ``"all"`` sentinel."""
    if targets is None:
        return True
    if isinstance(targets, str):
        return targets.strip().lower() == "all"
    if not targets:
        return True
    return len(targets) == 1 and isinstance(targets[0], str) and targets[0].strip().lower() == "all"
