# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` and ``doctor`` tools."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..services.probing import probe_all, verify_live


async def capabilities_tool(app: AppContext) -> str:
    """List every known CLI: whether it is installed, its auth status, and its models."""
    statuses = await probe_all(app.registry, default_model_for=app.config.default_model_for)
    return tool_success(statuses)


async def doctor_tool(app: AppContext, *, live: bool = True) -> str:
    """Health-probe every adapter, verifying auth that cannot be checked cheaply.

    `doctor` confirms each CLI is installed and reads its auth state. Some CLIs (Antigravity) have
    no non-interactive auth check, so their cheap state is ``unknown``. With ``live=True`` (the
    default), any installed adapter that is still ``unknown`` is verified with a minimal read-only
    round trip and reclassified by the outcome -- the only trustworthy signal when there is no
    ``whoami``. That spends a small model call for each such adapter; pass ``live=False`` for a
    metadata-only check with no model calls (``capabilities`` is the always-cheap snapshot).
    """
    if app.probe_cache is not None:
        app.probe_cache.invalidate()  # a diagnostic run wants fresh probes, not cached metadata
    statuses = await probe_all(app.registry, diagnostic=True, default_model_for=app.config.default_model_for)
    if live:
        statuses = await verify_live(
            app.delegation, statuses, correlation_id_factory=app.new_correlation_id, base_depth=app.base_depth
        )
    payload = {
        "adapters": statuses,
        "depth": app.base_depth,
        "max_depth": app.config.max_depth,
        "max_targets": app.config.max_targets,
        "max_concurrency": app.config.max_concurrency,
        "default_safety_mode": app.config.default_safety_mode,
    }
    return tool_success(payload)
