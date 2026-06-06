# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``list_roles`` tool."""

from __future__ import annotations

from ..context import AppContext, tool_success


async def list_roles_tool(app: AppContext) -> str:
    """List the available role personas (name, display name, description, source)."""
    roles = [
        {
            "name": role.name,
            "display_name": role.display_name,
            "description": role.description,
            "source": role.source,
        }
        for role in app.roles.all()
    ]
    return tool_success(roles)
