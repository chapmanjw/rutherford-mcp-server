# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` and ``doctor`` tools: list the ACP agents, and probe whether they actually drive."""

from __future__ import annotations

import asyncio
from typing import Any

from ..acp.conformance import probe_agent, probe_connection
from ..acp.descriptors import AgentDescriptor
from ..acp.effort import supports_effort
from ..context import AppContext, tool_success
from .common import ensure_known_agent

#: Local-model runtimes whose FIRST prompt can be slow (the model loads from disk on a cold start), so a
#: doctor probe budgets them more generously than a cloud agent -- otherwise a cold-but-healthy local model
#: false-flags as broken at the cloud default. Keyed off the provider the backend/auto-detect stamps.
_LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio"})
#: The probe budget for a local-model agent (cold start headroom). Applied as a floor over the call's
#: ``timeout_s``, so an explicit larger ``timeout_s`` still wins and a cloud agent keeps the shorter default.
_LOCAL_PROBE_TIMEOUT_S = 180.0

#: Static notes for MCP agent-users: capabilities never spawns agents, so live model catalogs stay on doctor.
_CAPABILITIES_NOTES: tuple[str, ...] = (
    "Model resolution: explicit model -> agent default_model -> agent-native default.",
    "Live advertised model ids: doctor(agent=<id>, connect_only=true).",
)


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


def _model_selection(descriptor: AgentDescriptor) -> str:
    """How Rutherford applies a resolved model for this agent (static; not a live channel probe)."""
    # * Launch-flag agents (Cursor) pass the model on argv; everyone else uses in-session ACP selection.
    return "launch_argv" if descriptor.model_launch_flag else "in_session"


def _agent_capability(descriptor: AgentDescriptor) -> dict[str, Any]:
    """One registry row for ``capabilities``: identity plus static model/effort roster fields."""
    return {
        "id": descriptor.id,
        "display_name": descriptor.display_name,
        "command": " ".join(descriptor.command),
        "provider": descriptor.provider,
        "default_model": descriptor.default_model,
        "fallback_model": descriptor.fallback_model,
        "model_selection": _model_selection(descriptor),
        "effort_capable": supports_effort(descriptor),
    }


async def capabilities_tool(app: AppContext) -> str:
    """Return the registered ACP agents as a cheap static roster (no spawn).

    Each agent includes id, display name, launch command, provider, configured ``default_model`` /
    ``fallback_model``, how Rutherford selects models (``launch_argv`` vs ``in_session``), and whether
    ``effort`` has a known knob. Live advertised model catalogs are not included -- use
    ``doctor(agent=<id>, connect_only=true)`` for that.
    """
    agents = [_agent_capability(descriptor) for descriptor in app.descriptors.all()]
    return tool_success({"agents": agents, "notes": list(_CAPABILITIES_NOTES)})


async def doctor_tool(
    app: AppContext, *, agent: str | None = None, timeout_s: float = 60.0, connect_only: bool = False
) -> str:
    """Probe each agent (or one named ``agent``) with a real read-only ACP round trip and report conformance.

    The only trustworthy health signal for an ACP agent: whether it spawns, handshakes, and answers. Probes
    run in parallel; each report says ok / no_answer / model_unavailable / handshake_failed / not_installed /
    error. ``model_unavailable`` means spawn + handshake succeeded (the ACP transport is reachable) but the
    harness/provider rejected the model on the turn -- a model/provider config issue (e.g. a Claude Code
    pointed at AWS Bedrock / Vertex), not a broken agent -- so a recognizable model rejection is not reported
    as a generic failure.
    ``timeout_s`` is the per-agent budget; a LOCAL-model agent (Ollama / LM Studio) gets a generous floor over
    it, because a cold local model loads on its first prompt and the cloud default would false-flag it.

    ``connect_only`` runs the LIGHTER handshake-only check instead: it opens a session (spawn + handshake, no
    prompt) and reports ``reachable`` / ``handshake_failed`` / ``not_installed`` plus the agent's advertised
    models -- proving Rutherford can talk to and configure the agent without a model call. Useful for an agent
    that connects but cannot complete a turn for a reason outside ACP (an auth / entitlement / quota issue,
    e.g. Grok without a SuperGrok subscription), which the full probe would report as a turn ``error``.
    """
    if agent is not None:
        ensure_known_agent(app.descriptors, agent)
        descriptors = [app.descriptors.get(agent)]
    else:
        descriptors = app.descriptors.all()
    if connect_only:
        connect_reports = await asyncio.gather(
            *(
                probe_connection(descriptor, timeout_s=_probe_timeout(descriptor, timeout_s))
                for descriptor in descriptors
            )
        )
        return tool_success({"agents": list(connect_reports)})
    reports = await asyncio.gather(
        *(probe_agent(descriptor, timeout_s=_probe_timeout(descriptor, timeout_s)) for descriptor in descriptors)
    )
    return tool_success({"agents": list(reports)})
