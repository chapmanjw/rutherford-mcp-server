# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` tool: list the ACP agents Rutherford can drive."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success


async def capabilities_tool(app: AppContext) -> str:
    """Return the registered ACP agents (id, display name, launch command, provider)."""
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
