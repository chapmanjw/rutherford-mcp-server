# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The shared on-disk config scopes for panels and custom roles.

Panels (``panels.toon``) and custom roles (``roles/``) are discovered in the same three scopes and
follow the same precedence, so both go through this one helper. The scopes, lowest precedence first:

1. ``~/.rutherford/`` -- your global, per-user config.
2. ``<cwd>/.rutherford/`` -- the project you are working in; overrides home for a same-named item.
3. ``$RUTHERFORD_CONFIG_DIR`` -- an explicit directory; overrides both.

The closest scope wins a name collision, which mirrors how the TOML config treats a project
``rutherford.toml`` over the global ``config.toml``. Callers read each scope in order and let a
later one override an earlier one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: The per-user config directory name placed under the home directory and the project root.
CONFIG_DIRNAME = ".rutherford"


def home_dir(env: Mapping[str, str]) -> Path:
    """The user's home directory, honoring an injected environment for testability."""
    raw = env.get("USERPROFILE") or env.get("HOME")
    return Path(raw) if raw else Path.home()


def config_scopes(
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> list[tuple[str, Path]]:
    """The config scope base directories, lowest precedence first (a later one overrides earlier).

    Each entry is ``(source, directory)`` where ``source`` is ``user`` | ``project`` | ``env``.
    ``$RUTHERFORD_CONFIG_DIR`` is included only when it is set.
    """
    environ = os.environ if env is None else env
    working_dir = Path.cwd() if cwd is None else Path(cwd)
    scopes: list[tuple[str, Path]] = [
        ("user", home_dir(environ) / CONFIG_DIRNAME),
        ("project", working_dir / CONFIG_DIRNAME),
    ]
    config_dir = environ.get("RUTHERFORD_CONFIG_DIR")
    if config_dir:
        scopes.append(("env", Path(config_dir)))
    return scopes
