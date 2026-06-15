# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Import a Zed / Cline ``acp.json`` ``agent_servers`` block as Rutherford agent config.

Zed and Cline declare ACP agents in an ``acp.json`` (or an editor ``settings.json``) under an
``agent_servers`` map: each entry is ``{"command": ..., "args": [...], "env": {...}}`` keyed by a display
name. The fields map one-to-one onto Rutherford's :class:`~rutherford.config.schema.AgentConfig`, so an
existing editor config carries over: the loader discovers an ``acp.json`` beside the TOML config (and in the
project's ``.rutherford/``) and folds its agents in, with the native TOML taking precedence at the same
scope. Use :func:`agents_from_acp_json` directly to parse an already-loaded document.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .schema import AgentConfig

_SLUG_NON_ID = re.compile(r"[^a-z0-9_-]+")


def agents_from_acp_json(data: Mapping[str, Any]) -> dict[str, AgentConfig]:
    """Parse an ``acp.json``-shaped document's ``agent_servers`` into ``{agent_id: AgentConfig}``.

    Tolerant of a malformed document: a non-mapping ``agent_servers``, a non-mapping entry, or an entry
    with no ``command`` is skipped rather than raised, so a partly-broken import does not block startup.
    """
    servers = data.get("agent_servers")
    if not isinstance(servers, Mapping):
        return {}
    agents: dict[str, AgentConfig] = {}
    for name, entry in servers.items():
        if not isinstance(entry, Mapping):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            continue
        raw_args = entry.get("args")
        args = [str(arg) for arg in raw_args] if isinstance(raw_args, (list, tuple)) else []
        raw_env = entry.get("env")
        env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, Mapping) else {}
        agents[_slug(str(name))] = AgentConfig(command=[command, *args], env=env)
    return agents


def _slug(name: str) -> str:
    """Turn an ``agent_servers`` display key (e.g. ``"Custom Agent"``) into an agent id (``custom_agent``)."""
    slug = _SLUG_NON_ID.sub("_", name.strip().lower()).strip("_")
    return slug or "agent"
