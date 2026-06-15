# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``discover`` tool: find installed ACP agents via the community registry and propose config for them.

The registry-driven counterpart to ``setup``. It fetches the ACP agent registry (cached for offline use),
detects which of those agents are ALREADY installed on this machine (PATH + curated install dirs, never
downloading), probes the ones it finds with a real read-only ACP round trip, and proposes a
``[agents.<id>]`` config block for every new agent that drives. Detection and probing are read-only; writing
the proposed config is opt-in (``write=True``) and never clobbers an existing ``[agents.<id>]`` section.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from ..acp.discovery import DiscoveredAgent, discover_agents
from ..acp.registry import CACHE_FILENAME, DEFAULT_REGISTRY_URL, RegistryError, fetch_registry
from ..config.loader import PROJECT_CONFIG_NAMES, default_global_config_path
from ..config.locations import CONFIG_DIRNAME, home_dir
from ..context import AppContext, tool_success
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError

#: The scopes ``discover --write`` understands (same as ``setup``); project is the default so a write never
#: silently lands in the shared global config.
SCOPES = ("global", "project")
#: Env override for the registry URL (a ``file://`` fixture in tests, or a pinned snapshot).
_REGISTRY_URL_ENV = "RUTHERFORD_ACP_REGISTRY_URL"
#: A registry id is only proposed when it is a safe bare TOML key: a different/hostile id (with ``.``, ``]``,
#: a newline, or TOML syntax) could otherwise write a different table or corrupt the config, so it is dropped.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


async def discover_tool(
    app: AppContext,
    *,
    refresh: bool = False,
    probe: bool = True,
    write: bool = False,
    scope: str = "project",
    probe_timeout_s: float = 45.0,
) -> str:
    """Fetch the ACP registry, detect installed agents, probe them, and propose config for the new ones.

    ``probe`` (default on) drives each found agent with a real read-only ACP round trip -- the trustworthy
    signal -- so the proposal only includes agents that actually answer; ``probe=False`` returns the raw
    detection without spawning anything. ``write`` (default off) appends the proposed ``[agents.<id>]``
    sections to the config for ``scope`` (``project`` -> ``<cwd>/.rutherford/config.toml``, ``global`` -> the
    platform config path), creating the file if needed and skipping any id whose section already exists --
    it never overwrites. ``refresh`` re-fetches the registry (the network is tried first regardless; the
    on-disk cache is the offline fallback).
    """
    if scope not in SCOPES:
        options = ", ".join(SCOPES)
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown scope {scope!r}; choose one of: {options}")

    url = os.environ.get(_REGISTRY_URL_ENV, DEFAULT_REGISTRY_URL)
    cache_path = home_dir(os.environ) / CONFIG_DIRNAME / CACHE_FILENAME
    try:
        agents, source = fetch_registry(url=url, cache_path=cache_path, force_refresh=refresh)
    except RegistryError as exc:
        raise RutherfordError(ErrorCode.INTERNAL, str(exc)) from exc

    discovered = await discover_agents(
        agents, known_ids=set(app.descriptors.ids()), probe=probe, probe_timeout_s=probe_timeout_s
    )
    # A proposal is a NEW agent (not already in the roster) that drives -- or, when probing is off, any new
    # agent found on disk (the caller opted out of the trustworthy signal, so we cannot vouch for it). An id
    # that is not a safe bare TOML key is never proposed (it could otherwise corrupt the written config).
    eligible = [d for d in discovered if not d.already_in_roster and (d.status == "ok" or not probe)]
    unsafe_ids = sorted({d.id for d in eligible if not _SAFE_ID.match(d.id)})
    proposals = _unique_by_id(d for d in eligible if _SAFE_ID.match(d.id))

    result: dict[str, Any] = {
        "registry_source": source,
        "registry_agents": len(agents),
        "discovered": [_row(d) for d in discovered],
        "new_drivers": [d.id for d in proposals],
    }
    if unsafe_ids:
        result["skipped_unsafe_ids"] = unsafe_ids
    proposed_config = _proposed_config(proposals)
    if proposed_config:
        result["proposed_config"] = proposed_config

    if write and proposals:
        outcome = _write_config(scope, proposals)
        result.update(outcome)
    elif write:
        result["written"] = False
        result["note"] = "no new agents to write"
    return tool_success(result)


def _unique_by_id(agents: Any) -> list[DiscoveredAgent]:
    """Keep the first :class:`DiscoveredAgent` per id, so a duplicate registry entry is not proposed twice."""
    seen: set[str] = set()
    unique: list[DiscoveredAgent] = []
    for agent in agents:
        if agent.id not in seen:
            seen.add(agent.id)
            unique.append(agent)
    return unique


def _row(d: DiscoveredAgent) -> dict[str, Any]:
    """One uniform row for the ``discovered`` table (keeps the TOON array decodable)."""
    return {
        "id": d.id,
        "name": d.name,
        "command": " ".join(d.command),
        "found_at": d.found_at,
        "status": d.status or "not_probed",
        "new": not d.already_in_roster,
    }


def _proposed_config(proposals: list[DiscoveredAgent]) -> str:
    """Render the ``[agents.<id>]`` config block for every proposed agent (empty string when none)."""
    if not proposals:
        return ""
    sections = [_agent_section(d) for d in proposals]
    header = "# Discovered ACP agents (review before keeping). Each was found installed and verified to drive."
    return header + "\n\n" + "\n\n".join(sections)


def _agent_section(d: DiscoveredAgent) -> str:
    """The ``[agents.<id>]`` TOML for one discovered agent: a comment, the section, and its launch command.

    The id is a pre-validated safe bare key (see ``_SAFE_ID``); the comment text (name, path) is collapsed to
    a single line so a newline in a registry name or a weird path cannot escape the ``#`` comment.
    """
    command = ", ".join(_toml_str(part) for part in d.command)
    return f"# {_one_line(d.name)} -- found at {_one_line(d.found_at)}\n[agents.{d.id}]\ncommand = [{command}]"


def _write_config(scope: str, proposals: list[DiscoveredAgent]) -> dict[str, Any]:
    """Append the proposed sections to the scope's config, skipping ids already present; never clobbering.

    The existing config is parsed (TOML) to find which agent ids it already defines, so an agent already
    configured is reported as skipped and never re-written -- a robust check, not a substring scan. If the
    existing file is not valid TOML it is left entirely untouched (a write would risk compounding the damage).
    Proposals are de-duplicated by id so a registry with two entries for one id cannot append a duplicate
    table. Returns the write outcome (path, written ids, skipped ids).
    """
    path = default_global_config_path() if scope == "global" else _project_config_target(Path.cwd())
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    existing_ids, parse_ok = _existing_agent_ids(existing)
    if not parse_ok:
        return {
            "written": False,
            "write_path": str(path),
            "scope": scope,
            "note": "the existing config is not valid TOML; left untouched -- fix it, then re-run with write",
        }
    written_ids: list[str] = []
    skipped_ids: list[str] = []
    new_sections: list[str] = []
    seen: set[str] = set()
    for d in proposals:
        if d.id in seen:
            continue  # a duplicate registry entry for the same id -- write the section once
        seen.add(d.id)
        if d.id in existing_ids:
            skipped_ids.append(d.id)
            continue
        written_ids.append(d.id)
        new_sections.append(_agent_section(d))
    if new_sections:
        block = "\n\n# Discovered ACP agents (added by `discover --write`).\n" + "\n\n".join(new_sections) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(block)
    return {
        "written": bool(written_ids),
        "write_path": str(path),
        "scope": scope,
        "written_ids": written_ids,
        "skipped_existing": skipped_ids,
    }


def _project_config_target(cwd: Path) -> Path:
    """The project config file to APPEND to: the one the loader will actually read, else a new config.toml.

    The loader reads the FIRST existing of :data:`PROJECT_CONFIG_NAMES` and stops (it ``break``s), so a write
    must land in that same file -- appending to ``.rutherford/config.toml`` when a ``rutherford.toml`` exists
    would be silently ignored. When no project config exists yet, default to ``.rutherford/config.toml``.
    """
    for name in PROJECT_CONFIG_NAMES:
        candidate = cwd / name
        if candidate.exists():
            return candidate
    return cwd / CONFIG_DIRNAME / "config.toml"


def _existing_agent_ids(existing: str) -> tuple[set[str], bool]:
    """The agent ids already defined in an existing config, and whether it parsed as valid TOML.

    ``(ids, True)`` for an empty or valid config (ids from its ``[agents]`` table); ``(set(), False)`` when the
    file is present but not valid TOML, signalling the caller to leave it untouched.
    """
    if not existing.strip():
        return set(), True
    try:
        parsed = tomllib.loads(existing)
    except (tomllib.TOMLDecodeError, ValueError):
        return set(), False
    agents = parsed.get("agents")
    return (set(agents) if isinstance(agents, dict) else set()), True


def _one_line(value: str) -> str:
    """Collapse any whitespace (including newlines) to single spaces so text stays on one comment line."""
    return re.sub(r"\s+", " ", value).strip()


#: TOML basic-string named escapes; every other control char (U+0000-001F and U+007F) is emitted as \uXXXX.
_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_str(value: str) -> str:
    """Quote ``value`` as a VALID TOML basic string, escaping every char that would otherwise break it.

    Command parts come from the registry, so an arg containing a newline / tab / control char (or a backslash
    or quote) must be escaped or the appended TOML is corrupt. Named escapes for the common controls; any
    other control char becomes a ``\\uXXXX`` escape, matching the TOML basic-string grammar.
    """
    out: list[str] = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
