# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``reload_panels`` tool and the shared panel-resolution helper.

Panels are loaded lazily and cached for the server's lifetime. ``reload_panels`` re-reads every
``panels.toon`` so a user can pick up edits without restarting the server. ``resolve_panel`` is the
one place the tool layer turns a panel name (plus optional one-off overrides) into a validated
:class:`~rutherford.config.panels.Panel`, so consensus, debate, and review all behave identically.
"""

from __future__ import annotations

from typing import Any

from ..config.panels import Panel
from ..context import AppContext, tool_success
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError


async def reload_panels_tool(app: AppContext) -> str:
    """Re-read every panels file and report the panels now available (raises if any file is invalid)."""
    store = app.panels.reload()
    names = store.names()
    return tool_success({"reloaded": True, "count": len(names), "panels": names})


def resolve_panel(app: AppContext, name: str, overrides: dict[str, Any] | None) -> Panel:
    """Resolve a saved panel by name, applying optional one-off overrides."""
    return app.panels.resolve(name, overrides)


def panel_for_call(
    app: AppContext,
    name: str,
    overrides: dict[str, Any] | None,
    targets: object,
    stances: object,
) -> Panel:
    """Resolve a panel for a tool call, rejecting ``targets``/``stances`` passed alongside it.

    A panel already names its seats and their steering, so combining it with an explicit ``targets``
    or ``stances`` argument is ambiguous and rejected with ``INVALID_INPUT``. The returned panel's
    ``to_targets()`` gives the seats (each carrying its own stance) and ``strategy`` the aggregation.
    """
    if targets is not None:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "panel and targets are mutually exclusive; use one or the other")
    if stances is not None:
        raise RutherfordError(
            ErrorCode.INVALID_INPUT, "panel and stances are mutually exclusive; set each seat's stance in the panel"
        )
    return resolve_panel(app, name, overrides)
