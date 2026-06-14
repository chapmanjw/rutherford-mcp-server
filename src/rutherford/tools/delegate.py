# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``delegate`` tool: hand a task to one ACP agent and return its normalized result."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.models import DelegationRequest, Target
from .common import ensure_known_agent, resolve_safety_mode


async def delegate_tool(
    app: AppContext,
    *,
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    trust_workspace: bool = False,
) -> str:
    """Validate the request, drive one ACP turn, and return the TOON-encoded result envelope."""
    ensure_known_agent(app.descriptors, cli)
    mode = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=prompt,
        working_dir=working_dir,
        files=list(files) if files else [],
        safety_mode=mode,
        timeout_s=timeout_s,
        trust_workspace=trust_workspace,
    )
    result = await app.delegation.delegate(request)
    return tool_success(result)
