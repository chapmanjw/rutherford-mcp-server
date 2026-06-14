# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``debate`` tool: several ACP agents argue a question across rounds, each on a persistent session."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import DebateRequest
from .common import as_target, ensure_known_targets, resolve_safety_mode


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
) -> str:
    """Validate the panel, run the multi-round debate over persistent sessions, and return the transcript."""
    parsed = [as_target(target) for target in (targets or [])]
    ensure_known_targets(app.descriptors, parsed)
    mode = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    judge_target = as_target(judge) if judge is not None else None
    request = DebateRequest(
        targets=parsed,
        prompt=prompt,
        rounds=rounds,
        working_dir=working_dir,
        safety_mode=mode,
        synthesize=synthesize,
        timeout_s=timeout_s,
        judge=judge_target,
    )
    result = await app.debate.debate(request)
    return tool_success(result)
