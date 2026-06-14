# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``reload_panels`` tool and the shared panel-resolution helper.

Panels are loaded lazily and cached for the server's lifetime. ``reload_panels`` re-reads every
``panels.toon`` so a user can pick up edits without restarting the server. ``panel_for_call`` is the one
entry point through which the tool layer turns a panel name (plus optional one-off overrides) into a
validated :class:`~rutherford.config.panels.Panel`: it also enforces the ``targets`` / ``stances`` mutual
exclusion, so consensus, debate, and review all behave identically.
"""

from __future__ import annotations

from typing import Any

from ..config.panels import Panel
from ..context import AppContext, tool_success
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError


async def reload_panels_tool(app: AppContext) -> str:
    """Re-read every panels file and report the panels now available (raises if any file is invalid).

    Returns ``{reloaded, count, panels: [{name, description, target_count}]}`` so a caller sees what was
    loaded. A malformed panels file raises ``PANEL_INVALID`` here, on the request path, naming the file and
    seat -- so a bad edit is caught the moment it is reloaded rather than at the next panel call.
    """
    store = app.panels.reload()
    panels = [
        {"name": panel.name, "description": panel.description, "target_count": len(panel.targets)}
        for panel in store.all()
    ]
    return tool_success({"reloaded": True, "count": len(panels), "panels": panels})


def panel_for_call(
    app: AppContext,
    name: str,
    overrides: dict[str, Any] | None,
    targets: object,
    stances: object,
) -> Panel:
    """Resolve a panel for a tool call, rejecting ``targets`` / ``stances`` passed alongside it.

    A panel already names its seats and their steering, so combining it with an explicit ``targets`` or
    ``stances`` argument is ambiguous and rejected with ``INVALID_INPUT``. The returned panel's
    ``to_targets()`` gives the seats (each carrying its own stance) and ``strategy`` the aggregation.
    """
    if targets is not None:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "panel and targets are mutually exclusive; use one or the other")
    if stances is not None:
        raise RutherfordError(
            ErrorCode.INVALID_INPUT, "panel and stances are mutually exclusive; set each seat's stance in the panel"
        )
    return app.panels.resolve(name, overrides)
