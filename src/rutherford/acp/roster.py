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

import json
from collections.abc import Callable

from ..config.schema import AgentConfig, RutherfordConfig
from ..domain.errors import ConfigError
from .descriptors import HIGH_FIDELITY, AgentDescriptor, DescriptorRegistry


def build_registry(config: RutherfordConfig) -> DescriptorRegistry:
    """Assemble the agent registry: built-in defaults, then config overrides / additions / filters.

    When ``auto_detect_local_models`` is set, a probe of any running Ollama / LM Studio adds a
    ``goose``-based agent per suitable model -- but at the LOWEST precedence: a built-in or explicit
    ``[agents.<id>]`` of the same id always wins, and a detected id never overwrites it. Detection is
    bounded and never raises, so a backend being down cannot break registry build.
    """
    resolved: dict[str, AgentDescriptor] = {descriptor.id: descriptor for descriptor in HIGH_FIDELITY}
    if config.auto_detect_local_models:
        from .local_detect import detect_local_agents  # lazy: local_detect imports this module's env builders

        for detected in detect_local_agents():
            resolved.setdefault(detected.id, detected)  # built-ins win; never overwrite
    for agent_id, entry in config.agents.items():
        if not entry.enabled:
            resolved.pop(agent_id, None)
            continue
        resolved[agent_id] = _merge(agent_id, entry, resolved.get(agent_id))
    if config.enabled_agents is not None:
        allow = set(config.enabled_agents)
        resolved = {agent_id: descriptor for agent_id, descriptor in resolved.items() if agent_id in allow}
    return DescriptorRegistry(resolved.values())


#: Built-in agents by id, for resolving ``base`` / built-in overrides.
_BUILTINS: dict[str, AgentDescriptor] = {descriptor.id: descriptor for descriptor in HIGH_FIDELITY}


def _merge(agent_id: str, entry: AgentConfig, existing: AgentDescriptor | None) -> AgentDescriptor:
    """Build a descriptor for ``agent_id`` from a config ``entry``, inheriting from a built-in when one applies.

    The launch command comes from ``entry.command``, else the ``base`` built-in (clone), else the built-in
    of the same id (override). A ``backend`` layers the local-runtime provider env on top.
    """
    source = _resolve_source(agent_id, entry, existing)
    if entry.command is not None:
        command = tuple(entry.command)
    elif source is not None:
        command = source.command
    else:
        raise ConfigError(
            f"agent '{agent_id}' is not a built-in agent and has no 'command' or 'base' to launch it; "
            "add a command (the ACP-server launch argv), a base (a built-in to clone), or remove the entry"
        )
    env = _backend_env(agent_id, entry, source) if entry.backend is not None else {}
    env.update(entry.env)  # an explicit env wins over the backend defaults
    # When this entry INHERITS a built-in's launch command (no raw ``command`` override), it also inherits that
    # built-in's wrapped-adapter identity (``underlying_cli`` / ``adapter_package``) -- so a clone or override of
    # a wrapped-adapter agent (codex / claude_code / pi) is still recognized AS that adapter: ``doctor``'s npm
    # install hint, and the Bedrock/Vertex model-env normalization gate, both key off it. A raw ``command``
    # override launches something else entirely, so it inherits neither (they stay ``None``).
    # Inherit the built-in's wrapped-adapter identity ONLY when this entry also inherits its launch command.
    inherits_launch = entry.command is None and source is not None
    return AgentDescriptor(
        id=agent_id,
        display_name=source.display_name if source is not None else agent_id,
        command=(*command, *entry.extra_args),
        provider=_first(entry.provider, entry.backend, source.provider if source is not None else None),
        env_passthrough=source.env_passthrough if source is not None else None,
        default_model=_first(entry.model, entry.default_model, source.default_model if source is not None else None),
        handshake_timeout_s=entry.handshake_timeout_s
        if entry.handshake_timeout_s is not None
        else (source.handshake_timeout_s if source is not None else 30.0),
        env_overrides=tuple(env.items()),
        fallback_model=_first(entry.fallback_model, source.fallback_model if source is not None else None),
        underlying_cli=source.underlying_cli if (inherits_launch and source is not None) else None,
        adapter_package=source.adapter_package if (inherits_launch and source is not None) else None,
        # Effort is a capability of the launched adapter, not the agent id: a clone that inherits a built-in's
        # launch command (``base=`` / ``backend=``, i.e. no explicit ``command``) records that built-in as its
        # effort lineage so ``effort_overrides`` resolves the right knob. A clone with its own ``command`` is
        # arbitrary argv (possibly a ``sh -c`` wrapper) -- no knowable lineage, so it stays an honest no-op.
        effort_base=source.id if (inherits_launch and source is not None) else None,
        # Launch-flag model selection is likewise a property of the inherited launch command (Cursor's
        # ``--model``), not of the config id -- a raw ``command=`` override drops it with the other quirks.
        model_launch_flag=source.model_launch_flag if (inherits_launch and source is not None) else None,
    )


def _resolve_source(agent_id: str, entry: AgentConfig, existing: AgentDescriptor | None) -> AgentDescriptor | None:
    """The built-in descriptor this entry inherits launch + quirks from: an explicit ``base``, else the
    same-id built-in being overridden, else ``None`` for a brand-new agent."""
    if entry.base is not None:
        if entry.base not in _BUILTINS:
            raise ConfigError(f"agent '{agent_id}' has base '{entry.base}', which is not a built-in agent")
        return _BUILTINS[entry.base]
    if entry.backend is not None and existing is None:
        raise ConfigError(f"agent '{agent_id}' sets a local 'backend' but has no 'base'; add e.g. base = \"goose\"")
    return existing


#: The default endpoint per local backend (``host:port``).
_LOCAL_DEFAULT_HOST = {"ollama": "localhost:11434", "lmstudio": "localhost:1234"}


def _goose_native(model: str, host: str) -> dict[str, str]:
    return {"GOOSE_PROVIDER": "ollama", "GOOSE_MODEL": model, "OLLAMA_HOST": host}


def _goose_openai(model: str, host: str) -> dict[str, str]:
    return {
        "GOOSE_PROVIDER": "openai",
        "GOOSE_MODEL": model,
        "OPENAI_HOST": f"http://{host}",
        "OPENAI_BASE_PATH": "v1/chat/completions",
        "OPENAI_API_KEY": "local",
    }


def _openai_compatible(model: str, host: str) -> dict[str, str]:
    return {"OPENAI_BASE_URL": f"http://{host}/v1", "OPENAI_API_KEY": "local", "OPENAI_MODEL": model}


def _anthropic_compatible(model: str, host: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": f"http://{host}",
        "ANTHROPIC_AUTH_TOKEN": "local",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
    }


def _opencode_openai(model: str, host: str) -> dict[str, str]:
    """Point opencode at a local OpenAI-compatible runtime via an inline ``OPENCODE_CONFIG_CONTENT``.

    opencode is config-driven, but it reads a whole config as JSON from ``OPENCODE_CONFIG_CONTENT`` (not just
    a file path), so a custom provider can be declared entirely in the environment -- one source of truth, no
    file on disk. The provider uses opencode's ``@ai-sdk/openai-compatible`` adapter pointed at the runtime's
    ``/v1`` endpoint, with the requested model as its single (default) model so opencode selects it without an
    explicit ``set_model``. The provider id is the backend name so the config reads naturally; the model key is
    the bare runtime model id (e.g. ``qwen3:8b`` / ``openai/gpt-oss-20b``). Proven live on both runtimes.
    """
    provider = "ollama" if ":11434" in host else "lmstudio"
    config = {
        "provider": {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": f"{provider} (local)",
                "options": {"baseURL": f"http://{host}/v1"},
                "models": {model: {"name": model}},
            }
        }
    }
    return {"OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":"))}


#: How each supported (base agent, backend) pair is pointed at a local runtime, every entry proven live
#: (2026-06-14 vetting against Ollama qwen3:8b and LM Studio openai/gpt-oss-20b). ``goose`` reaches Ollama
#: natively and LM Studio via its openai provider. ``qwen`` and ``opencode`` use the OpenAI-compatible ``/v1``
#: endpoint both runtimes expose -- ``opencode`` via an inline ``OPENCODE_CONFIG_CONTENT`` provider block.
#: ``claude_code`` needs an Anthropic-compatible endpoint, which Ollama provides (``/v1/messages``) but
#: LM Studio (OpenAI-only) does not, so it supports ``ollama`` only; it is also slow over a local model (drive
#: it with a generous timeout and a capable model). ``codex`` and ``hermes`` are NOT here: codex's custom
#: providers now require the OpenAI Responses API wire (``wire_api="responses"``), which neither runtime
#: speaks, and the codex-acp adapter is auth-gated; hermes' ``acp`` reads its config.yaml provider and ignores
#: the inference-provider env, so a local hermes is a config-file change, not an env-keyed pair.
_BACKEND_ENV: dict[tuple[str, str], Callable[[str, str], dict[str, str]]] = {
    ("goose", "ollama"): _goose_native,
    ("goose", "lmstudio"): _goose_openai,
    ("qwen", "ollama"): _openai_compatible,
    ("qwen", "lmstudio"): _openai_compatible,
    ("opencode", "ollama"): _opencode_openai,
    ("opencode", "lmstudio"): _opencode_openai,
    ("claude_code", "ollama"): _anthropic_compatible,
}


def _backend_env(agent_id: str, entry: AgentConfig, source: AgentDescriptor | None) -> dict[str, str]:
    """The provider env that points the ``base`` agent at a local model runtime (Ollama / LM Studio).

    Keyed by ``(base, backend)``: an unsupported pair (e.g. ``claude_code`` + ``lmstudio``) is a clear
    config error. ``model`` is validated non-empty by the schema.
    """
    base_id = entry.base or agent_id
    backend = entry.backend or ""
    builder = _BACKEND_ENV.get((base_id, backend))
    if builder is None:
        supported = ", ".join(sorted(f"{base}+{kind}" for base, kind in _BACKEND_ENV))
        raise ConfigError(
            f"agent '{agent_id}': base '{base_id}' does not support the '{backend}' local backend; "
            f"supported pairs: {supported}"
        )
    return builder(entry.model or "", entry.host or _LOCAL_DEFAULT_HOST[backend])


def _first(*values: str | None) -> str | None:
    """The first non-None value, or ``None``."""
    return next((value for value in values if value is not None), None)
