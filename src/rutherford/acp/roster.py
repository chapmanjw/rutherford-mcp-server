# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Build the agent registry from the built-in roster plus user config.

The built-in :data:`~rutherford.acp.descriptors.HIGH_FIDELITY` roster is the set of known agents Rutherford
ships -- curated launch commands and quirks (the Windows npm-shim resolution, per-agent handshake budgets,
the fixed provider) that a bare ``acp.json`` cannot express, so they work with zero config. Config
(``[agents.<id>]``) layers on top: it overrides a known agent's fields, disables one with ``enabled =
false``, or DEFINES a brand-new agent (any id not in the built-ins, which must supply a launch ``command``).
``enabled_agents`` finally restricts the result to an explicit allowlist. This is the config-driven path
that, under ACP, replaces a hand-written adapter.
"""

from __future__ import annotations

from ..config.schema import AgentConfig, RutherfordConfig
from ..domain.errors import ConfigError
from .descriptors import HIGH_FIDELITY, AgentDescriptor, DescriptorRegistry


def build_registry(config: RutherfordConfig) -> DescriptorRegistry:
    """Assemble the agent registry: built-in defaults, then config overrides / additions / filters."""
    resolved: dict[str, AgentDescriptor] = {descriptor.id: descriptor for descriptor in HIGH_FIDELITY}
    for agent_id, entry in config.agents.items():
        if not entry.enabled:
            resolved.pop(agent_id, None)
            continue
        resolved[agent_id] = _merge(agent_id, entry, resolved.get(agent_id))
    if config.enabled_agents is not None:
        allow = set(config.enabled_agents)
        resolved = {agent_id: descriptor for agent_id, descriptor in resolved.items() if agent_id in allow}
    return DescriptorRegistry(resolved.values())


def _merge(agent_id: str, entry: AgentConfig, base: AgentDescriptor | None) -> AgentDescriptor:
    """Build a descriptor for ``agent_id`` from a config ``entry`` over an optional built-in ``base``."""
    command = tuple(entry.command) if entry.command is not None else (base.command if base is not None else ())
    if not command:
        raise ConfigError(
            f"agent '{agent_id}' is not a built-in agent and has no 'command' to launch it; "
            "add a command (the ACP-server launch argv) or remove the entry"
        )
    return AgentDescriptor(
        id=agent_id,
        display_name=base.display_name if base is not None else agent_id,
        command=(*command, *entry.extra_args),
        provider=entry.provider if entry.provider is not None else (base.provider if base is not None else None),
        env_passthrough=base.env_passthrough if base is not None else None,
        default_model=entry.default_model
        if entry.default_model is not None
        else (base.default_model if base is not None else None),
        handshake_timeout_s=entry.handshake_timeout_s
        if entry.handshake_timeout_s is not None
        else (base.handshake_timeout_s if base is not None else 30.0),
        env_overrides=tuple(entry.env.items()),
    )
