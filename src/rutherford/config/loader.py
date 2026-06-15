# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Configuration loading: discover, merge, and validate.

A global config file is merged with an optional project-local override (the project wins), then
a small set of environment variables overrides specific values, then the result is validated
against :class:`~rutherford.config.schema.RutherfordConfig`. Invalid configuration raises
:class:`~rutherford.domain.errors.ConfigError` with a readable, multi-line detail.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain.errors import ConfigError
from .acp_json import agents_from_acp_json
from .schema import RutherfordConfig

_log = logging.getLogger(__name__)

#: Project-local config filenames searched in the working directory, in order (first found wins). The
#: ``.rutherford/config.toml`` form lives under the same project ``.rutherford/`` dir as jobs and panels
#: and is what ``setup ... scope=project`` writes, so a workspace persistence default takes effect there.
PROJECT_CONFIG_NAMES = ("rutherford.toml", ".rutherford.toml", ".rutherford/config.toml")
#: The project-local ``acp.json`` (Zed/Cline ``agent_servers`` import), discovered under ``.rutherford/``.
PROJECT_ACP_JSON = ".rutherford/acp.json"


def has_project_config(cwd: Path) -> bool:
    """Whether ``cwd`` holds a project-local Rutherford config under any of the recognized names.

    The single source of truth for "is this workspace configured" -- it honors every name in
    :data:`PROJECT_CONFIG_NAMES` (``rutherford.toml`` / ``.rutherford.toml`` / ``.rutherford/config.toml``),
    not just the ``setup``-written one, so a caller that keys UI off it stays correct if the set grows.
    """
    return any((cwd / name).exists() for name in PROJECT_CONFIG_NAMES)


def default_global_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the platform-appropriate global config path.

    Windows uses ``%APPDATA%\\rutherford\\config.toml``; other platforms use
    ``$XDG_CONFIG_HOME/rutherford/config.toml`` (falling back to ``~/.config``).
    """
    environ = os.environ if env is None else env
    if os.name == "nt":
        base = environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "rutherford" / "config.toml"
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "rutherford" / "config.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file into a dict, or raise :class:`ConfigError`."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    # UnicodeDecodeError: a non-UTF-8 file, e.g. the UTF-16 that Windows PowerShell 5.1
    # redirection writes by default -- a malformed config, not an internal crash.
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"could not parse config at {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config at {path}: {exc}") from exc


def _read_acp_json_agents(path: Path) -> dict[str, Any]:
    """Read an ``acp.json`` and project its ``agent_servers`` as a ``{"agents": {...}}`` config fragment.

    Best-effort, because an ``acp.json`` is an OPTIONAL import from another tool (Zed/Cline), not
    Rutherford's own config: a malformed file is logged and skipped rather than blocking startup (unlike a
    malformed TOML config, which is a hard error). An imported agent whose id collides with a built-in is
    skipped, so an auto-import never silently overrides a curated built-in launch -- override one explicitly
    in ``[agents.<id>]`` instead. Returns a fragment so it deep-merges under the TOML ``agents`` (the native
    config wins at the same scope). Only command/env are emitted, so the import stays minimal.
    """
    try:
        with path.open("rb") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        _log.warning("ignoring malformed acp.json at %s: %s", path, exc)
        return {}
    if not isinstance(raw, Mapping):
        return {}
    from ..acp.descriptors import HIGH_FIDELITY  # local import: avoid a config <-> acp import cycle

    builtin_ids = {descriptor.id for descriptor in HIGH_FIDELITY}
    fragment: dict[str, Any] = {}
    for agent_id, config in agents_from_acp_json(raw).items():
        if agent_id in builtin_ids:
            _log.warning("acp.json agent %r collides with a built-in; keeping the built-in launch", agent_id)
            continue
        fragment[agent_id] = config.model_dump(exclude_defaults=True, exclude_none=True)
    return {"agents": fragment} if fragment else {}


def deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base``. Nested dicts merge; other values replace."""
    result = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Build a config-fragment dict from the ``RUTHERFORD_*`` environment variables."""
    overrides: dict[str, Any] = {}
    if (value := env.get("RUTHERFORD_MAX_DEPTH")) is not None:
        overrides["max_depth"] = _as_int("RUTHERFORD_MAX_DEPTH", value)
    if (value := env.get("RUTHERFORD_MAX_TARGETS")) is not None:
        overrides["max_targets"] = _as_int("RUTHERFORD_MAX_TARGETS", value)
    if (value := env.get("RUTHERFORD_MAX_CONCURRENCY")) is not None:
        overrides["max_concurrency"] = _as_int("RUTHERFORD_MAX_CONCURRENCY", value)
    if (value := env.get("RUTHERFORD_DEFAULT_TIMEOUT_S")) is not None:
        overrides["default_timeout_s"] = _as_float("RUTHERFORD_DEFAULT_TIMEOUT_S", value)
    if (value := env.get("RUTHERFORD_DEFAULT_SAFETY")) is not None:
        overrides["default_safety_mode"] = value
    if (value := env.get("RUTHERFORD_TRUSTED_WORKSPACES")) is not None:
        overrides["trusted_workspaces"] = _split_paths(value)
    if (value := env.get("RUTHERFORD_ROLE_DIRS")) is not None:
        overrides["role_dirs"] = _split_paths(value)
    return overrides


def _as_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _as_float(name: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _split_paths(value: str) -> list[str]:
    return [part for part in value.split(os.pathsep) if part]


def load_config(
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    explicit_path: Path | str | None = None,
) -> RutherfordConfig:
    """Discover, merge, and validate configuration.

    Precedence (lowest to highest): a discovered ``acp.json`` import and the TOML config file at each
    scope (global, then project), then the ``RUTHERFORD_*`` environment overrides. At a scope the native
    TOML wins over an imported ``acp.json``; the project wins over the global. ``RUTHERFORD_CONFIG`` (or
    ``explicit_path``) replaces discovery with a single named file. A missing file is not an error.

    Security: project-scoped config (``.rutherford/config.toml`` and a discovered ``.rutherford/acp.json``)
    is trusted as code -- it can set an agent's launch ``command`` and subprocess ``env``. Discovery keys
    off the process working directory, so only start the server in a workspace you trust. (The
    trusted-workspace gate covers write/yolo *delegations*, not config discovery.)

    Raises:
        ConfigError: If a TOML config file cannot be parsed or the merged config fails validation. A
            malformed ``acp.json`` is logged and skipped, not raised (the import is best-effort).
    """
    environ = os.environ if env is None else env
    working_dir = Path.cwd() if cwd is None else Path(cwd)

    data: dict[str, Any] = {}

    explicit = explicit_path or environ.get("RUTHERFORD_CONFIG")
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ConfigError(f"RUTHERFORD_CONFIG points to a missing file: {path}")
        data = _read_toml(path)
    else:
        # Precedence low -> high: global acp.json, global TOML, project acp.json, project TOML. At each
        # scope the native TOML wins over an imported acp.json; the project wins over the global.
        global_path = default_global_config_path(environ)
        global_acp = global_path.parent / "acp.json"
        if global_acp.exists():
            data = deep_merge(data, _read_acp_json_agents(global_acp))
        if global_path.exists():
            data = deep_merge(data, _read_toml(global_path))
        project_acp = working_dir / PROJECT_ACP_JSON
        if project_acp.exists():
            data = deep_merge(data, _read_acp_json_agents(project_acp))
        for name in PROJECT_CONFIG_NAMES:
            project_path = working_dir / name
            if project_path.exists():
                data = deep_merge(data, _read_toml(project_path))
                break

    data = deep_merge(data, _env_overrides(environ))

    try:
        return RutherfordConfig.model_validate(data)
    except ValidationError as exc:
        detail = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"invalid configuration:\n{detail}") from exc
