# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``plan`` tool: ask one ACP agent for an implementation plan, built on delegate (read-only)."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.enums import SafetyMode
from ..domain.models import DelegationRequest, Target
from .common import apply_role, ensure_known_agent


async def plan_tool(
    app: AppContext,
    *,
    cli: str,
    goal: str,
    model: str | None = None,
    role: str = "architect",
    working_dir: str | None = None,
    files: list[str] | None = None,
    timeout_s: float | None = None,
) -> str:
    """Delegate planning of ``goal`` to one agent under the ``architect`` (planner) persona, read-only.

    Built on the delegation service with the ``architect`` built-in role prepended to ``goal``. Planning is
    CLAMPED to ``read_only`` -- the tool takes no ``safety_mode``, so a plan can never run with mutating
    permissions; implementing the plan is ``delegate`` in write mode by design. Returns the same
    ``DelegationResult`` envelope ``delegate`` does.
    """
    ensure_known_agent(app.descriptors, cli)  # a clean tool-boundary error before the role is applied
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=apply_role(app.roles, role, goal),
        role=role,
        working_dir=working_dir,
        files=files or [],
        safety_mode=SafetyMode.READ_ONLY,
        timeout_s=timeout_s,
    )
    result = await app.delegation.delegate(request, correlation_id=app.new_correlation_id())
    return tool_success(result)
