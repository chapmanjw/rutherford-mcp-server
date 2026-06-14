# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``consensus`` tool: ask the same prompt of several ACP agents in parallel and return every voice."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.models import ConsensusRequest
from .common import as_target, ensure_known_targets, resolve_safety_mode


async def consensus_tool(
    app: AppContext,
    *,
    prompt: str,
    targets: list[Any] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
) -> str:
    """Validate the panel, fan the prompt out across the targets, and return the TOON-encoded voices."""
    parsed = [as_target(target) for target in (targets or [])]
    ensure_known_targets(app.descriptors, parsed)
    mode = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    request = ConsensusRequest(
        targets=parsed,
        prompt=prompt,
        working_dir=working_dir,
        files=list(files) if files else [],
        safety_mode=mode,
        timeout_s=timeout_s,
    )
    result = await app.consensus.consensus(request)
    return tool_success(result)
