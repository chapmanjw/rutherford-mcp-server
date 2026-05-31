# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Roles: named personas backed by markdown, loaded as data, not code.

A role contributes a preamble (a system prompt) to a delegation. The built-in roles ship inside
the package (``rutherford/roles/*.md``) and load via :mod:`importlib.resources` in every install
mode; configured ``role_dirs`` add or override roles by name. Roles are version-controlled text,
so editing a persona never touches code.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError

#: The package subdirectory holding the built-in role markdown files.
_BUILTIN_PACKAGE = "rutherford"
_BUILTIN_SUBDIR = "roles"


class Role(BaseModel):
    """A named persona: metadata plus the preamble injected into a delegation."""

    name: str
    display_name: str
    description: str = ""
    preamble: str


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


def _parse_role(fallback_name: str, text: str) -> Role:
    """Build a :class:`Role` from a markdown document, using ``fallback_name`` if unset."""
    meta, body = _split_frontmatter(text)
    name = meta.get("name", fallback_name)
    return Role(
        name=name,
        display_name=meta.get("display_name", name.replace("_", " ").title()),
        description=meta.get("description", ""),
        preamble=body.strip(),
    )


def _builtin_roles() -> dict[str, Role]:
    """Load the role markdown files bundled in the package."""
    roles: dict[str, Role] = {}
    resource = importlib.resources.files(_BUILTIN_PACKAGE).joinpath(_BUILTIN_SUBDIR)
    for entry in resource.iterdir():
        if entry.name.endswith(".md"):
            role = _parse_role(entry.name.removesuffix(".md"), entry.read_text(encoding="utf-8"))
            roles[role.name] = role
    return roles


def load_roles(extra_dirs: Iterable[str | Path] = ()) -> RoleStore:
    """Load the built-in roles, then merge roles from ``extra_dirs`` (which override by name)."""
    roles = _builtin_roles()
    for directory in extra_dirs:
        path = Path(directory)
        if not path.is_dir():
            continue
        for file in sorted(path.glob("*.md")):
            role = _parse_role(file.stem, file.read_text(encoding="utf-8"))
            roles[role.name] = role
    return RoleStore(roles)
