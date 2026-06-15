# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``list_roles`` tool: enumerate the available role personas (id, name, description)."""

from __future__ import annotations

from ..context import AppContext, tool_success


async def list_roles_tool(app: AppContext) -> str:
    """Return every known role as ``{roles: [{id, name, description, source}]}`` (sorted by id).

    The catalog a caller reads before passing ``role="<id>"`` to ``delegate`` / ``consensus`` /
    ``debate``. ``source`` is the scope the role loaded from (``built-in`` | a ``role_dirs`` path | ``user`` |
    ``project`` | ``env``), so a caller can see which definition won an id collision. The prompt body is
    intentionally omitted -- it is the system prompt, not listing data.
    """
    roles = [
        {"id": role.id, "name": role.name, "description": role.description, "source": role.source}
        for role in app.roles.list()
    ]
    return tool_success({"roles": roles})
