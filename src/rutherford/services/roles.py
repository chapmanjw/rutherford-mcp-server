# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The role store: reusable persona / system-prompt definitions, loaded once at startup.

A "role" is a named system prompt a caller selects with ``role="<id>"`` on ``delegate`` /
``consensus`` / ``debate``; the role's prompt is prepended to the caller's task (see
:meth:`RoleStore.apply`), and ``list_roles`` enumerates the catalog. Built-in roles ship as package
data under ``rutherford/roles/*.md`` and are read via :mod:`importlib.resources`, so they resolve
from a source checkout or an installed wheel alike. Each ``config.role_dirs`` directory is also
scanned and may OVERRIDE a built-in of the same id, so a workspace can replace ``principal-reviewer``
with its own house standard.

Loading is tolerant by design: a malformed or unreadable role file is logged and skipped, never a
startup crash, because a bad persona must not take the whole server down. A role markdown file is a
small YAML-ish frontmatter block (``name`` and ``description``) followed by the body, which IS the
prompt; a missing or malformed frontmatter degrades to sensible defaults derived from the id and the
first line rather than failing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError

_log = logging.getLogger(__name__)

#: The package that ships the built-in role markdown files as package data.
_BUILTIN_PACKAGE = "rutherford.roles"
#: The delimiter between the prepended role prompt and the caller's task. Explicit and unambiguous so
#: an agent (and a reader) can tell the persona from the work.
_ROLE_DELIMITER = "\n\n---\n\n"


@dataclass(frozen=True, slots=True)
class Role:
    """One reusable persona: a stable ``id``, a human ``name`` and ``description``, and the ``prompt``.

    The ``prompt`` is the role's full system prompt (the markdown body of its source file). ``name``
    and ``description`` come from the file's frontmatter, defaulting from the id and first line when
    the frontmatter is absent or malformed.
    """

    id: str
    name: str
    description: str
    prompt: str


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a role file into its ``key: value`` frontmatter and its body.

    The frontmatter is an optional leading ``---`` ... ``---`` block of simple ``key: value`` lines
    (a deliberately tiny YAML subset -- no nesting, lists, or quoting machinery, so no YAML dependency
    is pulled in). A file with no frontmatter returns an empty mapping and the whole text as the body.
    A malformed block (an unterminated fence) is treated as "no frontmatter" rather than an error, so
    the body is never silently lost.
    """
    stripped = text.lstrip("﻿")  # tolerate a UTF-8 BOM on a Windows-authored file
    if not stripped.startswith("---"):
        return {}, text
    lines = stripped.splitlines()
    # The opening fence is line 0; find the closing fence.
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    return meta, body


def _role_from_text(role_id: str, text: str) -> Role:
    """Build a :class:`Role` from a source file's text, defaulting a missing name/description.

    ``name`` defaults to the id and ``description`` to the body's first non-empty line, so a role with
    no frontmatter is still usable and self-describing.
    """
    meta, body = _parse_frontmatter(text)
    prompt = body.strip()
    name = meta.get("name") or role_id
    description = meta.get("description") or _first_line(prompt)
    return Role(id=role_id, name=name, description=description, prompt=prompt)


def _first_line(text: str) -> str:
    """The first non-empty line of ``text``, the default role description when none is given."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class RoleStore:
    """An id -> :class:`Role` catalog: built-in package roles plus ``role_dirs`` overrides.

    Built once at startup and read-only thereafter. Built-ins load first; each directory in
    ``role_dirs`` is then scanned, and a role whose id matches an already-loaded one replaces it (a
    workspace override wins), so the layering is last-writer-wins in ``role_dirs`` order. Every load
    step is tolerant -- a missing directory or a malformed file is logged and skipped.
    """

    def __init__(self, role_dirs: list[str] | None = None) -> None:
        self._roles: dict[str, Role] = {}
        self._load_builtins()
        for directory in role_dirs or []:
            self._load_dir(directory)

    def _load_builtins(self) -> None:
        """Load every ``*.md`` shipped under the ``rutherford.roles`` package (via importlib.resources)."""
        try:
            root = resources.files(_BUILTIN_PACKAGE)
        except (ModuleNotFoundError, FileNotFoundError) as exc:  # pragma: no cover - defensive
            _log.warning("could not locate built-in roles package %r: %s", _BUILTIN_PACKAGE, exc)
            return
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if not entry.name.endswith(".md"):
                continue
            role_id = entry.name[: -len(".md")]
            try:
                text = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _log.warning("skipping unreadable built-in role %r: %s", role_id, exc)
                continue
            self._add(role_id, text, origin="built-in")

    def _load_dir(self, directory: str) -> None:
        """Load every ``*.md`` in one ``role_dirs`` directory; a role id here overrides a built-in."""
        path = Path(directory)
        if not path.is_dir():
            _log.warning("role_dirs entry is not a directory, skipping: %s", directory)
            return
        for file in sorted(path.glob("*.md")):
            role_id = file.stem
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _log.warning("skipping unreadable role file %s: %s", file, exc)
                continue
            self._add(role_id, text, origin=str(path))

    def _add(self, role_id: str, text: str, *, origin: str) -> None:
        """Parse ``text`` into a role and register it under ``role_id``, logging an override or a skip."""
        if not role_id:
            return
        try:
            role = _role_from_text(role_id, text)
        except Exception as exc:  # pragma: no cover - _role_from_text is defensive; belt and braces
            _log.warning("skipping malformed role %r from %s: %s", role_id, origin, exc)
            return
        if not role.prompt:
            _log.warning("skipping empty role %r from %s (no prompt body)", role_id, origin)
            return
        if role_id in self._roles:
            _log.info("role %r from %s overrides an earlier definition", role_id, origin)
        self._roles[role_id] = role

    def has(self, role_id: str) -> bool:
        """Whether ``role_id`` is a known role."""
        return role_id in self._roles

    def get(self, role_id: str) -> Role:
        """Return the role for ``role_id`` or raise ``UNKNOWN_ROLE`` listing the known ids."""
        role = self._roles.get(role_id)
        if role is None:
            known = ", ".join(sorted(self._roles)) or "(none)"
            raise RutherfordError(ErrorCode.UNKNOWN_ROLE, f"unknown role {role_id!r}; known roles: {known}")
        return role

    def list(self) -> list[Role]:
        """Every known role, sorted by id (a stable order for ``list_roles``)."""
        return [self._roles[role_id] for role_id in sorted(self._roles)]

    def apply(self, role_id: str, prompt: str) -> str:
        """Prepend role ``role_id``'s prompt to ``prompt`` with a clear delimiter; raise on a bad id.

        The composed string is the role's system prompt, then ``---``, then the caller's task -- so the
        persona governs the work without the two blurring together. Raises ``UNKNOWN_ROLE`` via
        :meth:`get` when the id is unknown, so a typoed role fails on the request path.
        """
        return f"{self.get(role_id).prompt}{_ROLE_DELIMITER}{prompt}"
