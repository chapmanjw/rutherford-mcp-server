# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``list_roles`` tool: enumerate the available role personas (id, name, description)."""

from __future__ import annotations

from ..context import AppContext, tool_success


async def list_roles_tool(app: AppContext) -> str:
    """Return every known role as ``{roles: [{id, name, description}]}`` (sorted by id).

    The catalog a caller reads before passing ``role="<id>"`` to ``delegate`` / ``consensus`` /
    ``debate``. The prompt body is intentionally omitted -- it is the system prompt, not listing data.
    """
    roles = [{"id": role.id, "name": role.name, "description": role.description} for role in app.roles.list()]
    return tool_success({"roles": roles})
