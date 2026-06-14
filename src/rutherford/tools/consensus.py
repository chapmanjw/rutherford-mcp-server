# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``consensus`` tool: ask the same prompt of several ACP agents in parallel and reduce the voices."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import ConsensusRequest, Target
from .common import (
    apply_role,
    as_target,
    ensure_known_agent,
    ensure_known_targets,
    parse_stances,
    parse_strategy,
    resolve_run_mode,
    resolve_safety_mode,
)
from .jobs import make_summary, submit_job


async def consensus_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | str | None = None,
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
    mode: str = "sync",
) -> str:
    """Validate the panel, fan the prompt out across the targets, and reduce the voices to a TOON envelope.

    ``targets`` is a list of ``{cli, model}`` objects (or ``cli`` / ``cli:model`` strings). Omit it, pass
    an empty list, pass the sentinel ``"all"``, or set ``expand_all=true`` to fan out to every registered
    agent (each at its default model, capped at ``max_targets``); the result's ``skipped`` field explains
    any agent left out. Optional ``stances`` (parallel to ``targets``) steer each voice for/against/neutral
    and cannot be combined with the auto-expanded panel. With a ``strategy`` other than ``all-voices``
    (optionally with a ``verdict_schema``), the voices are aggregated into a :class:`StrategyResult`
    outcome instead of returned individually. ``synthesize`` (tri-state; defaults to ``synthesize_default``,
    off out of the box) adds a server-side combined answer (``all-voices`` only); ``judge`` names the seat
    that writes it. ``mode="async"`` submits the panel as a background job and returns a ``job_id``;
    ``mode="sync"`` (the default) awaits it. Validation always runs on the request path, so a bad panel
    fails there rather than inside a job. A named ``role`` has its persona prepended to the prompt every
    voice receives; ``UNKNOWN_ROLE`` if the id is not a known role.
    """
    auto_panel = expand_all or _wants_all(targets)
    if auto_panel:
        parsed: list[Target] = []
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
    request = ConsensusRequest(
        targets=parsed,
        prompt=composed_prompt,
        stances=parse_stances(stances),
        working_dir=working_dir,
        files=list(files) if files else [],
        role=role,
        safety_mode=safety,
        synthesize=synthesize,
        timeout_s=timeout_s,
        expand_all=auto_panel,
        strategy=parse_strategy(strategy) if strategy is not None else parse_strategy("all-voices"),
        verdict_schema=verdict_schema,
        judge=judge_target,
    )

    async def run() -> str:
        result = await app.consensus.consensus(request)
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
