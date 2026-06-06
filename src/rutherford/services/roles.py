# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Roles: named personas backed by markdown or TOON, loaded as data, not code.

A role contributes a preamble (a system prompt) to a delegation. The built-in roles ship inside
the package (``rutherford/roles/*.md``) and load via :mod:`importlib.resources` in every install
mode. Custom roles are then layered on, lowest precedence first: configured ``role_dirs``, then the
well-known ``roles/`` directory in each config scope (``~/.rutherford``, then ``<cwd>/.rutherford``,
then ``$RUTHERFORD_CONFIG_DIR``). A later layer overrides an earlier one by name, so the closest
scope wins -- the same precedence panels use. Each role records its ``source`` so ``list_roles`` can
show where it came from. A role file is markdown (body is the preamble) or TOON (a ``system_prompt``
field); a malformed file is logged and skipped rather than crashing the server.
"""

from __future__ import annotations

import importlib.resources
import logging
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from ..config.locations import config_scopes
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..io.serialize import DecodeError, decode

_log = logging.getLogger(__name__)

#: The package subdirectory holding the built-in role markdown files.
_BUILTIN_PACKAGE = "rutherford"
_BUILTIN_SUBDIR = "roles"

#: The subdirectory under each config scope that holds custom role files.
_ROLES_SUBDIR = "roles"


class Role(BaseModel):
    """A named persona: metadata, the preamble injected into a delegation, and where it loaded from."""

    name: str
    display_name: str
    description: str = ""
    preamble: str
    #: Where the role was loaded from: ``builtin`` | ``config`` | ``user`` | ``project`` | ``env``.
    source: str = "builtin"


class RoleStore:
    """An in-memory name -> :class:`Role` mapping."""

    def __init__(self, roles: dict[str, Role]) -> None:
        self._roles = roles

    def get(self, name: str) -> Role:
        """Return the role named ``name`` or raise :class:`RutherfordError`."""
        try:
            return self._roles[name]
        except KeyError:
            known = ", ".join(self.names()) or "(none)"
            raise RutherfordError(
                ErrorCode.ROLE_NOT_FOUND,
                f"unknown role {name!r}; available roles: {known}",
            ) from None

    def has(self, name: str) -> bool:
        """Return whether a role named ``name`` is loaded."""
        return name in self._roles

    def names(self) -> list[str]:
        """Return the loaded role names, sorted."""
        return sorted(self._roles)

    def all(self) -> list[Role]:
        """Return every loaded role, ordered by name."""
        return [self._roles[name] for name in self.names()]


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown document into its simple ``key: value`` frontmatter and body.

    Recognizes a leading ``---`` fence. Only flat ``key: value`` pairs are parsed (no nested
    YAML), which is all the role files use. Without a fence, the whole text is the body.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    body_start = len(lines)
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            body_start = index + 1
            break
        key, sep, value = lines[index].partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    body = "\n".join(lines[body_start:])
    return meta, body


def _parse_markdown_role(fallback_name: str, text: str, source: str) -> Role:
    """Build a :class:`Role` from a markdown document, using ``fallback_name`` if unset."""
    meta, body = _split_frontmatter(text)
    name = meta.get("name", fallback_name)
    if not body.strip():
        raise ValueError("role body (the system prompt) is empty")
    return Role(
        name=name,
        display_name=meta.get("display_name", name.replace("_", " ").title()),
        description=meta.get("description", ""),
        preamble=body.strip(),
        source=source,
    )


def _parse_toon_role(fallback_name: str, text: str, source: str) -> Role:
    """Build a :class:`Role` from a TOON document with a ``system_prompt`` (or ``preamble``) field."""
    data = decode(text)
    if not isinstance(data, dict):
        raise ValueError("role file must be a TOON table")
    name = str(data.get("name", fallback_name))
    preamble = data.get("system_prompt") or data.get("preamble")
    if not isinstance(preamble, str) or not preamble.strip():
        raise ValueError("role needs a non-empty 'system_prompt'")
    return Role(
        name=name,
        display_name=str(data.get("display_name", name.replace("_", " ").title())),
        description=str(data.get("description", "")),
        preamble=preamble.strip(),
        source=source,
    )


def _load_role_file(file: Path, source: str) -> Role:
    """Parse one role file by extension. Raises on a malformed file (the caller logs and skips)."""
    text = file.read_text(encoding="utf-8")
    if file.suffix == ".toon":
        return _parse_toon_role(file.stem, text, source)
    return _parse_markdown_role(file.stem, text, source)


def _merge_dir(roles: dict[str, Role], directory: Path, source: str) -> None:
    """Merge every ``*.md`` / ``*.toon`` role in ``directory`` into ``roles``, overriding by name.

    A file that fails to parse is logged at warning level and skipped, so one bad role never stops
    the others (or the server) from loading.
    """
    if not directory.is_dir():
        return
    for file in sorted(directory.glob("*.md")) + sorted(directory.glob("*.toon")):
        try:
            role = _load_role_file(file, source)
        except (ValueError, DecodeError, OSError) as exc:
            _log.warning("skipping malformed role file %s: %s", file, exc)
            continue
        roles[role.name] = role


def _builtin_roles() -> dict[str, Role]:
    """Load the role markdown files bundled in the package."""
    roles: dict[str, Role] = {}
    resource = importlib.resources.files(_BUILTIN_PACKAGE).joinpath(_BUILTIN_SUBDIR)
    for entry in resource.iterdir():
        if entry.name.endswith(".md"):
            role = _parse_markdown_role(entry.name.removesuffix(".md"), entry.read_text(encoding="utf-8"), "builtin")
            roles[role.name] = role
    return roles


def load_roles(
    extra_dirs: Iterable[str | Path] = (),
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> RoleStore:
    """Load the built-in roles, then layer custom roles on top, the closest scope winning by name.

    Layers, lowest precedence first: the built-ins; the configured ``extra_dirs`` (``role_dirs``);
    then the ``roles/`` directory in each config scope (``~/.rutherford``, ``<cwd>/.rutherford``,
    ``$RUTHERFORD_CONFIG_DIR``). A later layer overrides an earlier one by role name. Each role keeps
    the ``source`` of the layer it came from.
    """
    roles = _builtin_roles()
    for directory in extra_dirs:
        _merge_dir(roles, Path(directory), "config")
    for source, base in config_scopes(env, cwd):
        _merge_dir(roles, base / _ROLES_SUBDIR, source)
    return RoleStore(roles)
