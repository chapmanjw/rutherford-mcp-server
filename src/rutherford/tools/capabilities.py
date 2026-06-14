# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` and ``doctor`` tools: list the ACP agents, and probe whether they actually drive."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..acp.conformance import probe_agent
from ..context import AppContext, tool_success
from .common import ensure_known_agent


async def capabilities_tool(app: AppContext) -> str:
    """Return the registered ACP agents (id, display name, launch command, provider) -- the cheap snapshot."""
    agents: list[dict[str, Any]] = [
        {
            "id": descriptor.id,
            "display_name": descriptor.display_name,
            "command": " ".join(descriptor.command),
            "provider": descriptor.provider,
        }
        for descriptor in app.descriptors.all()
    ]
    return tool_success({"agents": agents})


async def doctor_tool(app: AppContext, *, agent: str | None = None, timeout_s: float = 60.0) -> str:
    """Probe each agent (or one named ``agent``) with a real read-only ACP round trip and report conformance.

    The only trustworthy health signal for an ACP agent: whether it spawns, handshakes, and answers. Probes
    run in parallel; each report says working / no_answer / handshake_failed / not_installed / error.
    """
    if agent is not None:
        ensure_known_agent(app.descriptors, agent)
        descriptors = [app.descriptors.get(agent)]
    else:
        descriptors = app.descriptors.all()
    cwd = str(Path.cwd())
    reports = await asyncio.gather(
        *(probe_agent(descriptor, cwd=cwd, timeout_s=timeout_s) for descriptor in descriptors)
    )
    return tool_success({"agents": list(reports)})
