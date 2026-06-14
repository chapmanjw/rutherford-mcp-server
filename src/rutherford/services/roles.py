# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The role store: reusable persona / system-prompt definitions, loaded once at startup.

A "role" is a named system prompt a caller selects with ``role="<id>"`` on ``delegate`` /
``consensus`` / ``debate``; the role's prompt is prepended to the caller's task (see
:meth:`RoleStore.apply`), and ``list_roles`` enumerates the catalog. Built-in roles ship as package
data under ``rutherford/roles/*.md`` and are read via :mod:`importlib.resources`, so they resolve
from a source checkout or an installed wheel alike.

Custom roles are then layered on, lowest precedence first: each configured ``role_dirs`` directory, then
the well-known ``roles/`` directory in each config scope (``~/.rutherford``, then ``<cwd>/.rutherford``,
then ``$RUTHERFORD_CONFIG_DIR``). A later layer OVERRIDES an earlier one by id, so the closest scope wins
-- the same precedence panels use. Each role records its :attr:`Role.source` (which scope it came from) so
``list_roles`` can show where it loaded from. A role file is markdown (the body is the prompt) or TOON (a
``prompt`` / ``system_prompt`` field).

Loading is tolerant by design: a malformed or unreadable role file is logged and skipped, never a startup
crash, because a bad persona must not take the whole server down. A role markdown file is a small YAML-ish
frontmatter block (``name`` and ``description``) followed by the body, which IS the prompt; a missing or
malformed frontmatter degrades to sensible defaults derived from the id and the first line.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ..config.locations import config_scopes
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..io.serialize import DecodeError, decode

_log = logging.getLogger(__name__)

#: The package that ships the built-in role markdown files as package data.
_BUILTIN_PACKAGE = "rutherford.roles"
#: The subdirectory under each config scope that holds custom role files.
_ROLES_SUBDIR = "roles"
#: The delimiter between the prepended role prompt and the caller's task. Explicit and unambiguous so an
#: agent (and a reader) can tell the persona from the work.
_ROLE_DELIMITER = "\n\n---\n\n"


@dataclass(frozen=True, slots=True)
class Role:
    """One reusable persona: a stable ``id``, a human ``name`` and ``description``, the ``prompt``, and its ``source``.

    The ``prompt`` is the role's full system prompt (the body of its source file). ``name`` and
    ``description`` come from the file's frontmatter, defaulting from the id and first line when the
    frontmatter is absent or malformed. ``source`` records the scope the role loaded from: ``built-in`` for a
    packaged role, the ``role_dirs`` path, or ``user`` | ``project`` | ``env`` for a config-scope role.
    """

    id: str
    name: str
    description: str
    prompt: str
    source: str = "built-in"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a role file into its ``key: value`` frontmatter and its body.

    The frontmatter is an optional leading ``---`` ... ``---`` block of simple ``key: value`` lines (a
    deliberately tiny YAML subset -- no nesting, lists, or quoting machinery, so no YAML dependency is pulled
    in). A file with no frontmatter returns an empty mapping and the whole text as the body. A malformed block
    (an unterminated fence) is treated as "no frontmatter" rather than an error, so the body is never silently
    lost.
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


def _role_from_text(role_id: str, text: str, source: str = "built-in") -> Role:
    """Build a :class:`Role` from a markdown source file's text, defaulting a missing name/description.

    ``name`` defaults to the id and ``description`` to the body's first non-empty line, so a role with no
    frontmatter is still usable and self-describing.
    """
    meta, body = _parse_frontmatter(text)
    prompt = body.strip()
    name = meta.get("name") or role_id
    description = meta.get("description") or _first_line(prompt)
    return Role(id=role_id, name=name, description=description, prompt=prompt, source=source)


def _role_from_toon(role_id: str, text: str, source: str) -> Role:
    """Build a :class:`Role` from a TOON role file with a ``prompt`` (or ``system_prompt``) field.

    The TOON counterpart to a markdown role: a table whose ``prompt`` / ``system_prompt`` is the persona, with
    optional ``name`` and ``description``. Raises on a non-table file or an empty prompt (the caller logs and
    skips), so a malformed TOON role never reaches the catalog.
    """
    data = decode(text)
    if not isinstance(data, dict):
        raise ValueError("a TOON role file must be a table")
    prompt = data.get("prompt") or data.get("system_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a TOON role needs a non-empty 'prompt'")
    name = str(data.get("name") or role_id)
    description = str(data.get("description") or _first_line(prompt.strip()))
    return Role(id=role_id, name=name, description=description, prompt=prompt.strip(), source=source)


def _first_line(text: str) -> str:
    """The first non-empty line of ``text``, the default role description when none is given."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


class RoleStore:
    """An id -> :class:`Role` catalog: built-in package roles plus ``role_dirs`` and config-scope overrides.

    Built once at startup and read-only thereafter. Built-ins load first; then each directory in
    ``role_dirs``; then the ``roles/`` directory in each config scope (``~/.rutherford``, the project
    ``<cwd>/.rutherford``, then ``$RUTHERFORD_CONFIG_DIR``). A role whose id matches an already-loaded one
    replaces it, so the layering is last-writer-wins (the closest scope wins). Every load step is tolerant --
    a missing directory or a malformed file is logged and skipped.
    """

    def __init__(
        self,
        role_dirs: list[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
    ) -> None:
        self._roles: dict[str, Role] = {}
        self._load_builtins()
        for directory in role_dirs or []:
            self._load_dir(Path(directory), origin=str(Path(directory)))
        for source, base in config_scopes(env, cwd):
            self._load_dir(base / _ROLES_SUBDIR, origin=source)

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
            self._add(role_id, text, suffix=".md", origin="built-in")

    def _load_dir(self, path: Path, *, origin: str) -> None:
        """Load every ``*.md`` / ``*.toon`` in one role directory; a role id here overrides an earlier load.

        A missing directory is a silent skip (a config scope without a ``roles/`` dir is the common case, not
        an error). Files are loaded markdown-first then TOON, sorted within each kind, so the ordering is
        deterministic; a later file of the same id wins.
        """
        if not path.is_dir():
            return
        for file in sorted(path.glob("*.md")) + sorted(path.glob("*.toon")):
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _log.warning("skipping unreadable role file %s: %s", file, exc)
                continue
            self._add(file.stem, text, suffix=file.suffix, origin=origin)

    def _add(self, role_id: str, text: str, *, suffix: str, origin: str) -> None:
        """Parse ``text`` into a role and register it under ``role_id``, logging an override or a skip."""
        if not role_id:
            return
        try:
            if suffix == ".toon":
                role = _role_from_toon(role_id, text, origin)
            else:
                role = _role_from_text(role_id, text, origin)
        except (ValueError, DecodeError) as exc:
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
        persona governs the work without the two blurring together. Raises ``UNKNOWN_ROLE`` via :meth:`get`
        when the id is unknown, so a typoed role fails on the request path.
        """
        return f"{self.get(role_id).prompt}{_ROLE_DELIMITER}{prompt}"
