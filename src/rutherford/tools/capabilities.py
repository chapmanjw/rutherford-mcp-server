# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` and ``doctor`` tools."""

from __future__ import annotations

import asyncio

from ..context import AppContext, tool_success
from .probing import probe_adapter


async def capabilities_tool(app: AppContext) -> str:
    """List every known CLI: whether it is installed, its auth status, and its models.

    Probes run in worker threads so the metadata calls (version, list-models, auth-status) do not
    block the event loop.
    """
    adapters = app.registry.all()
    statuses = await asyncio.gather(*(asyncio.to_thread(probe_adapter, adapter) for adapter in adapters))
    return tool_success(list(statuses))


async def doctor_tool(app: AppContext) -> str:
    """Health-probe every adapter and report diagnostic notes for unavailable targets."""
    adapters = app.registry.all()
    statuses = await asyncio.gather(
        *(asyncio.to_thread(probe_adapter, adapter, diagnostic=True) for adapter in adapters)
    )
    payload = {
        "adapters": list(statuses),
        "depth": app.base_depth,
        "max_depth": app.config.max_depth,
        "max_targets": app.config.max_targets,
        "default_safety_mode": app.config.default_safety_mode,
    }
    return tool_success(payload)
