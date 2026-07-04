# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Normalize the launch environment for the Claude Code ACP adapter on a non-cloud provider.

The ``claude-agent-acp`` adapter (a third-party npm shim Rutherford cannot patch) resolves its model from
``ANTHROPIC_MODEL`` -> ``settings.model`` -> its first advertised alias (``default`` -> the bare cloud id
``claude-opus-4-8``). On a host configured for AWS Bedrock or Google Vertex (``CLAUDE_CODE_USE_BEDROCK`` /
``CLAUDE_CODE_USE_VERTEX``), that bare cloud id is rejected -- Bedrock needs an inference-profile id like
``us.anthropic.claude-opus-4-1-20250805-v1:0``. The standalone ``claude`` CLI works because it applies the
``env`` block of ``~/.claude/settings.json`` to itself; the adapter/SDK path does not use that as the model.

So when the host is on Bedrock/Vertex, Rutherford resolves a valid provider model id and injects it as
``ANTHROPIC_MODEL`` for the spawned adapter. On a STANDARD build the adapter does not override it: its
``resolveModelPreference`` returns no match for a raw provider id (it is version-incompatible with the
``default``/``sonnet``/``haiku`` aliases), so it never calls ``set_model`` and the id flows through to the SDK.
This is gated to the Claude Code adapter seat and to a Bedrock/Vertex host, so every other seat -- and a
normal API-key Claude Code -- is left exactly as it is.

CAVEAT (enterprise / Amazon Toolbox builds): an ENFORCED model allowlist (``enforceAvailableModels: true`` in
``settings.json``, which an org wrapper may rewrite on every launch) makes the adapter rewrite every model to a
BARE ALIAS and substring-match the injected ``ANTHROPIC_MODEL`` back DOWN to that alias before calling
``set_model`` -- so ``ANTHROPIC_MODEL`` alone is NOT enough there, and the rejected bare alias still reaches
Bedrock. The value that survives the allowlist rewrite is ``ANTHROPIC_CUSTOM_MODEL_OPTION`` (the adapter exempts
it). Rutherford does NOT auto-inject that one -- bypassing an org's enforced allowlist is an explicit opt-in --
so :func:`bedrock_remediation_hint` instead points the user at the per-agent ``[agents.<id>.env]`` config that
sets both vars. See ``docs/bedrock.md``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from ..config.locations import home_dir
from .descriptors import AgentDescriptor

_log = logging.getLogger(__name__)

#: Env values that mean a Bedrock/Vertex flag is OFF even though it is present (so ``=0`` does not enable it).
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _truthy(value: str | None) -> bool:
    """Whether an env flag like ``CLAUDE_CODE_USE_BEDROCK`` is set to an ON value."""
    return value is not None and value.strip().lower() not in _FALSEY


def _is_claude_adapter(descriptor: AgentDescriptor) -> bool:
    """Whether this seat launches the Claude Code ACP adapter (``claude-agent-acp``) -- the only one this applies to.

    Keyed SOLELY on the wrapped-adapter marker ``underlying_cli == "claude"``, which the built-in carries and
    the roster merge preserves across a same-id override and a ``base = "claude_code"`` clone -- but DROPS for a
    raw ``command`` override, which launches something else entirely. So it is deliberately NOT ``provider ==
    "anthropic"`` (too broad -- a future Anthropic-compatible seat) and NOT a bare ``id == "claude_code"``
    fallback (that would still catch a raw-command override that no longer runs the adapter, leaking
    Anthropic-specific env into a custom server). A local-runtime clone DOES keep the marker and (on a shell
    that happens to export the Bedrock flag) passes the gate too, but stays a NO-OP: its backend already sets
    ``ANTHROPIC_MODEL``, which the resolution precedence keeps as-is rather than overriding.
    """
    return descriptor.underlying_cli == "claude"


def _looks_like_provider_model(model: str | None) -> bool:
    """Whether ``model`` looks like a raw Bedrock/Vertex provider id rather than a Claude Code alias.

    A provider id carries a region/publisher path or a version separator -- a ``.`` (``us.anthropic.…`` /
    ``global.anthropic.…``), a ``:`` (``…-v1:0``), or a ``@`` (Vertex ``claude-opus-4-1@20250805``). The
    Claude Code aliases (``default`` / ``sonnet`` / ``haiku`` / ``opus``) and the bare cloud id
    ``claude-opus-4-8`` carry none, so a config ``default_model`` that is merely an alias is NOT promoted into
    ``ANTHROPIC_MODEL`` (promoting an alias would recreate the very bug this fixes).
    """
    if not model:
        return False
    return any(separator in model for separator in ".:@")


def claude_bedrock_env(descriptor: AgentDescriptor, env: Mapping[str, str], cwd: str) -> dict[str, str]:
    """The ``ANTHROPIC_MODEL`` (+ ``ANTHROPIC_SMALL_FAST_MODEL``) to inject for a Bedrock/Vertex Claude Code seat.

    Returns ``{}`` -- a clean no-op -- unless this is the Claude Code adapter seat AND the host has a
    Bedrock/Vertex flag set. The model id is resolved by precedence:

    1. an already-set ``ANTHROPIC_MODEL`` in ``env`` (the adapter already uses it; keep it, never clobber),
    2. the descriptor's ``default_model`` -- but ONLY when it looks like a raw provider id, never a Claude Code
       alias (a ``[agents.claude_code] model`` pinned to a real Bedrock id wins; an alias is ignored here),
    3. ``ANTHROPIC_DEFAULT_OPUS_MODEL`` in ``env`` (the Bedrock opus-tier id, promoted to the model the adapter
       actually reads),
    4. ``ANTHROPIC_MODEL`` then ``ANTHROPIC_DEFAULT_OPUS_MODEL`` in the ``env`` block of the host's
       ``~/.claude/settings.json`` and ``<cwd>/.claude/settings.json`` (project wins).

    When nothing resolves, returns ``{}`` (the turn still fails, but ``doctor`` then reports ``model_unavailable``
    with guidance rather than a generic error). ``ANTHROPIC_SMALL_FAST_MODEL`` is resolved similarly and OMITTED
    when nothing is found -- never defaulted to the main model, which would make background calls expensive and
    can fail where only the main inference profile is enabled.
    """
    if not _is_claude_adapter(descriptor):
        return {}
    if not (_truthy(env.get("CLAUDE_CODE_USE_BEDROCK")) or _truthy(env.get("CLAUDE_CODE_USE_VERTEX"))):
        return {}
    settings_env = _read_claude_settings_env(env, cwd)
    default_model = descriptor.default_model if _looks_like_provider_model(descriptor.default_model) else None
    main = (
        env.get("ANTHROPIC_MODEL")
        or default_model
        or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
        or settings_env.get("ANTHROPIC_MODEL")
        or settings_env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
    )
    if not main:
        return {}
    result = {"ANTHROPIC_MODEL": main}
    small = (
        env.get("ANTHROPIC_SMALL_FAST_MODEL")
        or env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
        or settings_env.get("ANTHROPIC_SMALL_FAST_MODEL")
        or settings_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
    )
    if small:
        result["ANTHROPIC_SMALL_FAST_MODEL"] = small
    return result


def _read_claude_settings_env(env: Mapping[str, str], cwd: str) -> dict[str, str]:
    """The merged ``env`` block of the host's Claude Code settings, lowest precedence first (project wins).

    Reads ``~/.claude/settings.json`` (user) then ``<cwd>/.claude/settings.json`` (project) -- the scopes the
    Claude Code CLI applies to its own environment. ``home_dir`` honors an injected ``HOME`` / ``USERPROFILE``
    so a test can point the user scope at a tmp dir. Tolerant by construction: a missing or malformed file, or
    a non-object ``env`` block, contributes nothing and never raises on the spawn path.
    """
    merged: dict[str, str] = {}
    for path in (home_dir(env) / ".claude" / "settings.json", Path(cwd) / ".claude" / "settings.json"):
        merged.update(_read_env_block(path))
    return merged


def _read_env_block(path: Path) -> dict[str, str]:
    """The string-valued ``env`` block of one Claude Code ``settings.json``, or ``{}`` (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # missing, unreadable, or invalid JSON -- normalization is best-effort, never fatal at spawn
    if not isinstance(data, dict):
        return {}
    block = data.get("env")
    if not isinstance(block, dict):
        return {}
    return {key: value for key, value in block.items() if isinstance(key, str) and isinstance(value, str)}


#: AWS Bedrock global/regional inference-profile id prefixes (paired with an ``anthropic`` segment). A model id
#: starting with one is a strong Bedrock signal even when no ``CLAUDE_CODE_USE_BEDROCK`` flag is visible.
_BEDROCK_PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.")


def is_bedrock_or_vertex_host(env: Mapping[str, str]) -> bool:
    """Whether the host environment indicates a Bedrock or Vertex Claude Code (the enterprise-wrapper case)."""
    return _truthy(env.get("CLAUDE_CODE_USE_BEDROCK")) or _truthy(env.get("CLAUDE_CODE_USE_VERTEX"))


def _looks_like_bedrock_profile(model: str | None) -> bool:
    """Whether ``model`` looks like an AWS Bedrock inference-profile id (``global.``/``us.``/... + anthropic)."""
    if not model:
        return False
    lower = model.lower()
    return "anthropic" in lower and any(lower.startswith(prefix) for prefix in _BEDROCK_PROFILE_PREFIXES)


def _is_invalid_model_signature(message: str) -> bool:
    """Whether a turn error looks like a provider rejecting the model id (the Bedrock/enterprise 400 class).

    Matches the exact AWS Bedrock phrasing ("the provided model identifier is invalid") and the more general
    shape of an HTTP 400 that names the model -- so a wrapper that reports the rejection slightly differently is
    still caught, without matching unrelated 400s.
    """
    lower = message.lower()
    if "model identifier is invalid" in lower or "provided model identifier" in lower:
        return True
    return (
        "400" in message
        and "model" in lower
        and any(tok in lower for tok in ("invalid", "identifier", "not supported"))
    )


def _hint_model_id(descriptor: AgentDescriptor, env: Mapping[str, str]) -> str | None:
    """A REAL provider model id to show in the remediation snippet, or ``None`` to use a placeholder.

    Only a value that looks like a raw provider id (or an explicit ``ANTHROPIC_CUSTOM_MODEL_OPTION`` the user
    already set) is offered -- never the rejected bare alias, which would just reproduce the failure if pasted.
    """
    for candidate in (
        env.get("ANTHROPIC_CUSTOM_MODEL_OPTION"),
        env.get("ANTHROPIC_MODEL"),
        descriptor.default_model,
        env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ):
        if _looks_like_provider_model(candidate):
            return candidate
    return None


def bedrock_remediation_hint(descriptor: AgentDescriptor, env: Mapping[str, str], message: str) -> str | None:
    """A targeted ``doctor`` remediation hint when a Claude Code seat's turn was rejected for its model id.

    Returns advice text -- never a mutation -- only when ALL hold: the seat is the Claude Code adapter
    (:func:`_is_claude_adapter`, so a custom-command or non-Claude seat is not handed Anthropic-specific
    advice), the turn error matches the invalid-model signature, and a Bedrock/Vertex indicator is present (a
    ``CLAUDE_CODE_USE_BEDROCK`` / ``CLAUDE_CODE_USE_VERTEX`` flag, or a model id that looks like a Bedrock
    inference profile). The hint describes the per-agent ``[agents.<id>.env]`` fix -- which lives outside the
    ``.claude`` tree, so an org wrapper that rewrites ``settings.json`` cannot revert it -- and sets
    ``ANTHROPIC_CUSTOM_MODEL_OPTION`` (exempt from the enforced-allowlist rewrite, the value that actually
    survives). ``None`` whenever it does not apply, so ``doctor`` stays read-only and quiet for everyone else.
    """
    if not _is_claude_adapter(descriptor):
        return None
    if not _is_invalid_model_signature(message):
        return None
    indicator = (
        is_bedrock_or_vertex_host(env)
        or _looks_like_bedrock_profile(descriptor.default_model)
        or _looks_like_bedrock_profile(env.get("ANTHROPIC_MODEL"))
        or _looks_like_bedrock_profile(env.get("ANTHROPIC_CUSTOM_MODEL_OPTION"))
        or _looks_like_bedrock_profile(env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"))
    )
    if not indicator:
        return None
    model = _hint_model_id(descriptor, env) or "<your provider model id, e.g. global.anthropic.claude-opus-4-8[1m]>"
    return (
        "Claude Code rejected the model id, which a Bedrock/Vertex (or enterprise-wrapped, e.g. Amazon Toolbox) "
        "build does when handed a bare cloud alias instead of a provider inference-profile id. Pin a valid id "
        f"for this seat in Rutherford's OWN config (outside the .claude tree, so an org wrapper that rewrites "
        f"settings.json cannot revert it):\n"
        f"  [agents.{descriptor.id}]\n"
        f'  default_model = "{model}"\n'
        f"  [agents.{descriptor.id}.env]\n"
        f'  ANTHROPIC_MODEL = "{model}"\n'
        f'  ANTHROPIC_CUSTOM_MODEL_OPTION = "{model}"\n'
        "ANTHROPIC_CUSTOM_MODEL_OPTION is exempt from an enforced model allowlist, so it survives where "
        "ANTHROPIC_MODEL alone is rewritten back to the rejected alias. See docs/bedrock.md."
    )
