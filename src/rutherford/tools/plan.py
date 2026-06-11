# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``plan`` tool: ask one target to produce an implementation plan, built on delegate."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.enums import SafetyMode
from ..domain.models import DelegationRequest, Target


async def plan_tool(
    app: AppContext,
    *,
    cli: str,
    goal: str,
    model: str | None = None,
    role: str = "planner",
    working_dir: str | None = None,
    files: list[str] | None = None,
    timeout_s: float | None = None,
) -> str:
    """Delegate planning of ``goal`` to one target with the ``planner`` role.

    Planning is CLAMPED to ``read_only`` -- the tool takes no ``safety_mode``, so a plan can never
    run with mutating adapter flags; implementing the plan is ``delegate`` in write mode by design.
    """
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=goal,
        role=role,
        working_dir=working_dir,
        files=files or [],
        safety_mode=SafetyMode.READ_ONLY,
        timeout_s=timeout_s,
    )
    result = await app.delegation.delegate(request, correlation_id=app.new_correlation_id(), base_depth=app.base_depth)
    return tool_success(result)
