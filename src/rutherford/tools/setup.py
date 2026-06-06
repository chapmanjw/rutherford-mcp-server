# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``setup`` tool: probe the installed CLIs and scaffold a starter config and panel."""

from __future__ import annotations

import asyncio
import os

from ..context import AppContext, tool_success
from ..services.setup import apply_setup_plan, build_setup_plan
from .probing import probe_adapter


async def setup_tool(
    app: AppContext,
    *,
    apply: bool = False,
    force: bool = False,
    safety_mode: str = "read_only",
    trusted_workspaces: list[str] | None = None,
    panel_name: str = "default",
) -> str:
    """Propose (or write) a starter config and panel from the CLIs you have installed and signed in.

    Probes every known CLI, recommends a starter panel from the ready ones, and prepares the files to
    write -- the main ``config.toml`` and a ``panels.toon``. By default this is a dry run: the plan
    (including the exact file contents) is returned so you can review it. Pass ``apply=true`` to write
    the files; an existing file is left untouched unless ``force=true``.
    """
    statuses = await asyncio.gather(*(asyncio.to_thread(probe_adapter, adapter) for adapter in app.registry.all()))
    plan = build_setup_plan(
        statuses,
        env=os.environ,
        safety_mode=safety_mode,
        trusted_workspaces=trusted_workspaces or [],
        panel_name=panel_name,
    )
    if apply:
        written = apply_setup_plan(plan, force=force)
        return tool_success({"applied": True, "written": written, "plan": plan})
    return tool_success({"applied": False, "plan": plan})
