# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Configuration loading: discover, merge, and validate.

A global config file is merged with an optional project-local override (the project wins), then
a small set of environment variables overrides specific values, then the result is validated
against :class:`~rutherford.config.schema.RutherfordConfig`. Invalid configuration raises
:class:`~rutherford.domain.errors.ConfigError` with a readable, multi-line detail.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain.errors import ConfigError
from .schema import RutherfordConfig

#: Project-local config filenames searched in the working directory, in order.
PROJECT_CONFIG_NAMES = ("rutherford.toml", ".rutherford.toml")


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
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"could not parse config at {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read config at {path}: {exc}") from exc


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

    Precedence (lowest to highest): the global config file, the project-local file, then the
    ``RUTHERFORD_*`` environment overrides. ``RUTHERFORD_CONFIG`` (or ``explicit_path``) replaces
    discovery with a single named file. A missing file is not an error -- defaults apply.

    Raises:
        ConfigError: If a config file cannot be parsed or the merged config fails validation.
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
        global_path = default_global_config_path(environ)
        if global_path.exists():
            data = _read_toml(global_path)
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
