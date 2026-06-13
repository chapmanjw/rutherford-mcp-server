# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``consensus`` tool: ask the same prompt of several CLIs in parallel."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.enums import DelegationMode
from ..domain.models import ConsensusRequest, Target
from ..services.delegation import ActivityCallback
from .common import (
    as_target,
    async_job_envelope,
    ensure_known_cli,
    ensure_known_targets,
    parse_effort,
    parse_mode,
    parse_on_budget,
    parse_stances,
    parse_strategy,
    resolve_safety_mode,
)
from .panels import panel_for_call


async def consensus_tool(
    app: AppContext,
    *,
    targets: list[Any] | str | None = None,
    prompt: str,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    strategy: str | None = None,
    verdict_schema: dict[str, Any] | None = None,
    judge: Any = None,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    harvest_partial: bool = False,
    mode: str = "sync",
    include_raw: bool = False,
    persist: bool | None = None,
    external_tracking: bool = False,
    on_activity: ActivityCallback | None = None,
) -> str:
    """Run ``prompt`` across several CLIs in parallel and return every voice.

    ``targets`` is a list of ``{cli, model}`` objects (or ``cli`` / ``cli:model`` strings). Omit it,
    pass an empty list, or pass the sentinel ``"all"`` to fan out to every installed + authenticated
    adapter (each at its default model, capped at ``max_targets``); the result's ``skipped`` field
    explains any adapter left out. Alternatively name a saved ``panel`` (with optional one-off
    ``panel_overrides``) instead of ``targets``; the two are mutually exclusive. Optional ``stances``
    (parallel to ``targets``) steer each voice for/against/neutral and cannot be combined with the
    auto-expanded panel. Optional ``synthesize`` adds a server-side combined answer; when omitted it
    defaults to the configured ``synthesize_default`` (false out of the box), and an explicit value
    always wins. With a ``strategy`` other than ``all-voices`` (optionally with a ``verdict_schema``), the
    voices are aggregated into an outcome instead of returned individually.

    ``effort`` (``low`` | ``medium`` | ``high`` | ``xhigh``) is the producer "how hard may each voice think"
    hint, mapped per adapter and reported as applied (F8a); omit it to follow ``default_effort``.
    ``time_budget_s`` is a wall-clock harvest deadline for the WHOLE panel, distinct from each voice's
    ``timeout_s``: at the deadline the answered voices are kept and in-flight ones are cut, and the panel
    aggregates over the harvest if ``min_quorum`` holds (else ``BUDGET_EXHAUSTED``). ``on_budget`` picks the
    disposition: ``harvest`` (default), ``continue`` (run all voices; budget advisory), or ``resume``.
    ``harvest_partial=true`` additionally re-prompts each cut voice whose session was recovered for a clean
    best answer at the cut (it spends extra budget, hence opt-in).
    """
    target_objs: list[Target]
    panel_strategy: str | None = None
    if panel is not None:
        resolved = panel_for_call(app, panel, panel_overrides, targets, stances)
        target_objs = resolved.to_targets()
        panel_strategy = resolved.strategy
        panel_stances = None  # each panel seat carries its own stance
        expand_all = False
    else:
        panel_stances = parse_stances(stances)
        expand_all = _wants_all(targets)
        if expand_all:
            target_objs = []
        elif isinstance(targets, str):
            target_objs = [as_target(targets)]  # a bare "cli" / "cli:model" string
        else:
            target_objs = [as_target(target) for target in targets or []]
    if not expand_all:
        ensure_known_targets(app.registry, target_objs)  # a clean tool-boundary error, not a buried voice
    judge_target = as_target(judge) if judge is not None else None
    if judge_target is not None:
        ensure_known_cli(app.registry, judge_target.cli)  # a typo'd judge is a clean error, not silent no-synthesis
    effective_strategy = parse_strategy(strategy if strategy is not None else (panel_strategy or "all-voices"))
    request = ConsensusRequest(
        targets=target_objs,
        prompt=prompt,
        stances=panel_stances,
        working_dir=working_dir,
        files=files or [],
        role=role,
        safety_mode=resolve_safety_mode(safety_mode, app.config.default_safety_mode),
        synthesize=synthesize,
        timeout_s=timeout_s,
        effort=parse_effort(effort),
        time_budget_s=time_budget_s,
        on_budget=parse_on_budget(on_budget),
        harvest_partial=harvest_partial,
        include_raw=include_raw,
        expand_all=expand_all,
        strategy=effective_strategy,
        verdict_schema=verdict_schema,
        judge=judge_target,
        persist=persist,
        external_tracking=external_tracking,
    )
    correlation_id = app.new_correlation_id()

    if parse_mode(mode) is DelegationMode.ASYNC:
        # ``set_interim`` carries the F8a ``on_budget=continue`` best-effort result to the job while the
        # stragglers keep running, so a poller sees the harvested-so-far set before the panel finishes.
        job = app.jobs.submit(
            "consensus",
            lambda progress, activity, set_interim: app.consensus.consensus(
                request,
                correlation_id=correlation_id,
                base_depth=app.base_depth,
                on_progress=progress,
                on_activity=activity,  # N1: the structured stream feeds the job's poll buffer (decision 3-K)
                on_interim_result=set_interim,
            ),
        )
        return tool_success(
            async_job_envelope(app, job, persist=persist, complex_run=True, external_tracking=external_tracking)
        )

    # Sync path only: push live progress via MCP (N1, item 3); an async job is polled, not pushed.
    result = await app.consensus.consensus(
        request, correlation_id=correlation_id, base_depth=app.base_depth, on_activity=on_activity
    )
    result.notice = app.persistence_notice(
        persisted=result.run_dir is not None, complex_run=True, external_tracking=external_tracking
    )
    return tool_success(result)


def _wants_all(targets: list[Any] | str | None) -> bool:
    """Whether the caller asked for the full panel: targets omitted, empty, or the ``"all"`` sentinel."""
    if targets is None:
        return True
    if isinstance(targets, str):
        return targets.strip().lower() == "all"
    if not targets:
        return True
    return len(targets) == 1 and isinstance(targets[0], str) and targets[0].strip().lower() == "all"
