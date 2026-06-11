# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``debate`` tool: several CLIs argue a question across multiple rounds."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.enums import DelegationMode, Stance
from ..domain.models import DebateRequest, Target
from .common import as_target, ensure_known_cli, ensure_known_targets, parse_mode, parse_stances, resolve_safety_mode
from .panels import panel_for_call


async def debate_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    rounds: int = 2,
    judge: Any = None,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool = True,
    timeout_s: float | None = None,
    mode: str = "sync",
    include_raw: bool = False,
) -> str:
    """Run a multi-round debate across several CLIs and return the full transcript.

    ``targets`` is a list of ``{cli, model}`` objects (or ``cli`` / ``cli:model`` strings); a debate
    needs at least two. Alternatively name a saved ``panel`` (with optional ``panel_overrides``)
    instead of ``targets``; the two are mutually exclusive. ``rounds`` (default 2) is how many passes
    the panel makes: round one is each voice's independent answer, and every later round shows a voice
    the others' latest positions and asks it to rebut and revise. Optional ``stances`` (parallel to
    ``targets``) keep a voice arguing for/against/neutral the whole way through. ``synthesize`` (on by
    default) adds a closing summary of where the panel landed. The result's ``rounds`` hold every
    voice's answer at every round, so the discussion is fully retraceable. With ``mode="async"`` a job
    id is returned.
    """
    target_objs: list[Target]
    debate_stances: list[Stance] | None
    if panel is not None:
        target_objs = panel_for_call(app, panel, panel_overrides, targets, stances).to_targets()
        debate_stances = None  # each panel seat carries its own stance
    else:
        target_objs = [as_target(target) for target in targets or []]
        debate_stances = parse_stances(stances)
    ensure_known_targets(app.registry, target_objs)  # a clean tool-boundary error, not a buried voice
    judge_target = as_target(judge) if judge is not None else None
    if judge_target is not None:
        ensure_known_cli(app.registry, judge_target.cli)  # a typo'd judge is a clean error, not silent no-synthesis
    request = DebateRequest(
        targets=target_objs,
        prompt=prompt,
        rounds=rounds,
        stances=debate_stances,
        working_dir=working_dir,
        files=files or [],
        role=role,
        safety_mode=resolve_safety_mode(safety_mode, app.config.default_safety_mode),
        synthesize=synthesize,
        timeout_s=timeout_s,
        include_raw=include_raw,
        judge=judge_target,
    )
    correlation_id = app.new_correlation_id()

    if parse_mode(mode) is DelegationMode.ASYNC:
        job = app.jobs.submit(
            "debate",
            lambda progress: app.debate.debate(
                request,
                correlation_id=correlation_id,
                base_depth=app.base_depth,
                on_progress=progress,
            ),
        )
        return tool_success({"job_id": job.id, "status": job.status, "kind": job.kind})

    result = await app.debate.debate(request, correlation_id=correlation_id, base_depth=app.base_depth)
    return tool_success(result)
