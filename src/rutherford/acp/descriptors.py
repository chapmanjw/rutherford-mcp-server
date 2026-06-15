# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Agent descriptors: the small declaration that replaces a hand-written subprocess adapter.

Under ACP the protocol negotiates output parsing, system prompts, file context, and resume, so an agent is
described by *how to launch it as an ACP server* plus a few quirks -- not a per-CLI parser. The registry is
a closed mapping that fails fast on an unknown id, mirroring the old adapter registry's contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    """How Rutherford launches and identifies one ACP agent."""

    id: str
    display_name: str
    #: The argv that launches this agent as an ACP server over stdio (e.g. ``("goose", "acp")``).
    command: tuple[str, ...]
    #: The model vendor when fixed (``"mistral"``), else ``None`` for a bring-your-own-model agent.
    provider: str | None = None
    #: Environment variables passed to the agent subprocess; ``None`` means the full inherited environment
    #: (the v1 default, so the agent's own credential discovery works). A curated allowlist replaces this
    #: once each agent's subprocess credential discovery is characterized by the conformance harness.
    env_passthrough: tuple[str, ...] | None = None
    #: The model id used when a call names none; ``None`` means the agent's own default.
    default_model: str | None = None
    #: Seconds allotted for the initialize + new_session handshake before it is judged failed. A heavyweight
    #: agent that sets up a workspace/runtime on new_session (e.g. OpenHands) needs more than the default.
    handshake_timeout_s: float = 30.0
    #: Environment variables to SET for the agent subprocess (name, value pairs), layered on top of the
    #: inherited / allowlisted environment. Sourced from a config ``[agents.<id>] env`` block. A tuple of
    #: pairs (not a dict) so the descriptor stays a frozen, hashable value object.
    env_overrides: tuple[tuple[str, str], ...] = ()
    #: The model to retry with when the requested model is unavailable (F7 model fallback). ``None`` -- the
    #: default for every built-in agent -- means the agent exposes no fallback model, so the model-fallback
    #: path is a clean no-op for it (it is NOT invented). Set per agent in config (``[agents.<id>]
    #: fallback_model``) for an agent whose plan can decline a named model and fall back to a known-good one.
    fallback_model: str | None = None


class DescriptorRegistry:
    """An immutable id -> descriptor mapping with fail-fast lookup."""

    def __init__(self, descriptors: Iterable[AgentDescriptor]) -> None:
        mapping: dict[str, AgentDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.id in mapping:
                raise ValueError(f"duplicate agent id {descriptor.id!r}")
            mapping[descriptor.id] = descriptor
        self._by_id = mapping

    def get(self, agent_id: str) -> AgentDescriptor:
        """Return the descriptor for ``agent_id`` or raise :class:`KeyError`."""
        return self._by_id[agent_id]

    def has(self, agent_id: str) -> bool:
        """Whether ``agent_id`` is registered."""
        return agent_id in self._by_id

    def ids(self) -> list[str]:
        """The registered agent ids, sorted."""
        return sorted(self._by_id)

    def all(self) -> list[AgentDescriptor]:
        """Every descriptor, ordered by id."""
        return [self._by_id[agent_id] for agent_id in self.ids()]

    def __len__(self) -> int:
        return len(self._by_id)


#: The high-fidelity native-ACP roster (research receipt 02-synthesis): the agents Rutherford drives
#: directly as ACP servers, in initial-onboarding order. ``goose``, ``opencode``, ``vibe``, ``junie``,
#: ``codex``, ``claude_code``, ``copilot``, ``qwen``, ``droid``, ``cursor``, ``kiro``, ``pi``, ``hermes``
#: and ``cline`` are confirmed live on this machine; the rest (``kimi``, ``openhands``) carry their
#: researched launch command and are gated by the conformance harness before they are trusted.
#:
#: ``codex`` and ``claude_code`` use the official Zed adapters -- ``codex-acp`` and ``claude-agent-acp`` (npm
#: ``@agentclientprotocol/*``) -- which front the Codex and Claude Code CLIs as ACP servers. Both honor the
#: existing CLI login over ACP and need no API key: ``codex-acp`` reuses the ChatGPT login
#: (``~/.codex/auth.json``) and ``claude-agent-acp`` reuses the Claude Code login (receipt
#: ``11-official-adapters-auth-test.md``). The launch command is the adapter shim, not the underlying CLI.
#:
#: ``copilot``/``droid``/``cursor``/``kiro``/``pi`` are bring-your-own-model (provider ``None``); ``qwen``
#: and ``hermes`` carry their vendor as an unconfirmed guess. ``cursor``'s ``acp`` subcommand is real but
#: hidden from ``--help``; ``kiro``'s ACP binary is ``kiro-cli`` (the ``kiro`` binary is the IDE launcher);
#: ``pi`` runs through the ``pi-acp`` wrapper (``npm i -g pi-acp``), which spawns ``pi --mode rpc``;
#: ``hermes`` depends on the configured Nous model (a slow one can blow a turn budget); ``cline`` drives
#: over ACP only with Cline's own service auth -- a ChatGPT-subscription or OpenRouter provider set in the
#: desktop app is NOT reached by the headless ``--acp`` path (it returns an empty turn). (``kilo`` is not
#: here: handshake works on OpenRouter, but the turn never completes headless; ``kimi``'s only ACP auth is
#: an interactive terminal device-code login. Receipts 12/13.)
#:
#: ``gemini`` is Google's official Gemini CLI (``gemini --acp``); live-verified driving over ACP on
#: 2026-06-14 (status=ok, ~2.2s), which supersedes the earlier "headless ACP known-issue" finding (fixed by
#: Gemini CLI 0.46.0). ``qoder`` is Qoder AI's ``qodercli`` (``qodercli --acp``; the ``--acp`` flag is hidden
#: from ``--help`` but real -- like ``cursor``'s ``acp``), live-verified driving on 2026-06-14 (status=ok,
#: ~2.9s). Qoder AI's own installer drops the binary at ``~/.qoder/bin/qodercli/`` (NOT on PATH), so on a
#: machine where ``qodercli`` is not PATH-resolvable, point the launch command at the full path via
#: ``[agents.qoder] command = [...]`` -- or let ``discover`` find it.
#:
#: ``grok`` is xAI's Grok CLI (``grok agent stdio``); ACP-native with ``--model`` / ``--reasoning-effort``
#: knobs. Connection-verified 2026-06-14: it spawns, handshakes, and opens a session (``doctor
#: connect_only`` reports ``reachable``), but a full turn needs a SuperGrok Heavy entitlement -- without it
#: the model call returns ``403 SuperGrok Heavy subscription required`` and ``doctor`` reports it as a turn
#: error. So it is wired up and reachable; it answers once the account is entitled.
HIGH_FIDELITY: tuple[AgentDescriptor, ...] = (
    AgentDescriptor("goose", "Goose", ("goose", "acp")),
    AgentDescriptor("opencode", "OpenCode", ("opencode", "acp")),
    AgentDescriptor("vibe", "Mistral Vibe", ("vibe-acp",), provider="mistral"),
    AgentDescriptor("cline", "Cline", ("cline", "--acp")),
    AgentDescriptor("junie", "Junie", ("junie", "--acp=true")),
    AgentDescriptor("kimi", "Kimi Code", ("kimi", "acp"), provider="moonshot"),
    AgentDescriptor("openhands", "OpenHands", ("openhands", "acp"), handshake_timeout_s=90.0),
    AgentDescriptor("codex", "Codex", ("codex-acp",), provider="openai"),
    AgentDescriptor("claude_code", "Claude Code", ("claude-agent-acp",), provider="anthropic"),
    AgentDescriptor("copilot", "GitHub Copilot", ("copilot", "--acp")),
    AgentDescriptor("qwen", "Qwen Code", ("qwen", "--acp"), provider="alibaba"),
    AgentDescriptor("droid", "Factory Droid", ("droid", "exec", "--output-format", "acp")),
    AgentDescriptor("cursor", "Cursor", ("cursor-agent", "acp")),
    AgentDescriptor("kiro", "Kiro", ("kiro-cli", "acp")),
    AgentDescriptor("pi", "Pi", ("pi-acp",)),
    AgentDescriptor("hermes", "Hermes", ("hermes", "acp"), provider="nous"),
    AgentDescriptor("gemini", "Gemini CLI", ("gemini", "--acp"), provider="google"),
    AgentDescriptor("qoder", "Qoder", ("qodercli", "--acp"), provider="qoder"),
    AgentDescriptor("grok", "Grok", ("grok", "agent", "stdio"), provider="xai"),
)


def default_registry() -> DescriptorRegistry:
    """Build the registry from the high-fidelity roster."""
    return DescriptorRegistry(HIGH_FIDELITY)
