# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``plan`` tool: ask one target to produce an implementation plan, built on delegate."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.models import DelegationRequest, Target
from .common import parse_safety_mode


async def plan_tool(
    app: AppContext,
    *,
    cli: str,
    goal: str,
    model: str | None = None,
    role: str = "planner",
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str = "read_only",
    timeout_s: float | None = None,
) -> str:
    """Delegate planning of ``goal`` to one target with the ``planner`` role (read-only)."""
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=goal,
        role=role,
        working_dir=working_dir,
        files=files or [],
        safety_mode=parse_safety_mode(safety_mode),
        timeout_s=timeout_s,
        depth=app.base_depth,
    )
    result = await app.delegation.delegate(request, correlation_id=app.new_correlation_id(), base_depth=app.base_depth)
    return tool_success(result)
