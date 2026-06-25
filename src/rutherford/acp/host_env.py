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
``ANTHROPIC_MODEL`` for the spawned adapter. The adapter does NOT override it: its ``resolveModelPreference``
returns no match for a raw provider id (it is version-incompatible with the ``default``/``sonnet``/``haiku``
aliases), so it never calls ``set_model`` and the id flows through to the SDK. This is gated to the Claude
Code adapter seat and to a Bedrock/Vertex host, so every other seat -- and a normal API-key Claude Code --
is left exactly as it is.
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
