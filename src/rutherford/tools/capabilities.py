# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` and ``doctor`` tools: list the ACP agents, and probe whether they actually drive."""

from __future__ import annotations

import asyncio
from typing import Any

from ..acp.conformance import probe_agent
from ..acp.descriptors import AgentDescriptor
from ..context import AppContext, tool_success
from .common import ensure_known_agent

#: Local-model runtimes whose FIRST prompt can be slow (the model loads from disk on a cold start), so a
#: doctor probe budgets them more generously than a cloud agent -- otherwise a cold-but-healthy local model
#: false-flags as broken at the cloud default. Keyed off the provider the backend/auto-detect stamps.
_LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio"})
#: The probe budget for a local-model agent (cold start headroom). Applied as a floor over the call's
#: ``timeout_s``, so an explicit larger ``timeout_s`` still wins and a cloud agent keeps the shorter default.
_LOCAL_PROBE_TIMEOUT_S = 180.0


def _probe_timeout(descriptor: AgentDescriptor, default: float) -> float:
    """The probe budget for one agent: a generous floor for a local-model agent, else the call default.

    A local agent is one whose provider is a local runtime (Ollama / LM Studio -- set by the backend builders
    and auto-detect), or whose env points at a localhost endpoint (a configured ``backend`` agent). Cold local
    models load on the first prompt, so the cloud-agent default reliably false-flags them; give them headroom.
    """
    if descriptor.provider in _LOCAL_PROVIDERS:
        return max(default, _LOCAL_PROBE_TIMEOUT_S)
    # URL hosts are case-insensitive, so a configured ``http://LOCALHOST:1234`` endpoint is local too.
    if any("localhost" in value.lower() or "127.0.0.1" in value for _, value in descriptor.env_overrides):
        return max(default, _LOCAL_PROBE_TIMEOUT_S)
    return default


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
    ``timeout_s`` is the per-agent budget; a LOCAL-model agent (Ollama / LM Studio) gets a generous floor over
    it, because a cold local model loads on its first prompt and the cloud default would false-flag it.
    """
    if agent is not None:
        ensure_known_agent(app.descriptors, agent)
        descriptors = [app.descriptors.get(agent)]
    else:
        descriptors = app.descriptors.all()
    reports = await asyncio.gather(
        *(probe_agent(descriptor, timeout_s=_probe_timeout(descriptor, timeout_s)) for descriptor in descriptors)
    )
    return tool_success({"agents": list(reports)})
