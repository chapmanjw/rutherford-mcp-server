# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Global trusted-workspace allowlist: add/remove a path in the platform ``config.toml``.

The write/yolo gate reads ``trusted_workspaces`` from the merged config. These helpers edit the
*global* file only (``default_global_config_path``), so a one-shot ``rutherford-mcp-server trust``
(equivalently ``python -m rutherford trust``) from a repo root registers that directory for every
server process that loads the global config. A project-local ``trusted_workspaces`` still replaces
(does not union) the global list at load time -- see :func:`~rutherford.config.loader.deep_merge`.

This is a HUMAN CLI path by construction: nothing under ``tools/`` imports it, so a model cannot extend
its own write allowlist. ``tests/test_trust.py`` pins that.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..domain.errors import ConfigError
from ..io.tomltext import toml_str
from .loader import default_global_config_path

#: Action reported by :func:`trust_workspace` / :func:`untrust_workspace`.
TrustAction = Literal["added", "removed", "unchanged", "missing"]

_ASSIGNMENT = re.compile(r"^(\s*)trusted_workspaces\s*=", re.MULTILINE)

#: The stable leading text of the managed header. Matched as a PREFIX when stripping, so a header written
#: by an older version (with different trailing wording) is still recognized and collapsed rather than
#: accumulating a second copy beside the current one.
_TRUST_HEADER_PREFIX = "# Absolute paths under which write/yolo delegations are permitted"

_TRUST_HEADER = f"{_TRUST_HEADER_PREFIX} (managed by `rutherford-mcp-server trust` / `untrust`).\n"


@dataclass(frozen=True, slots=True)
class TrustResult:
    """Outcome of a trust/untrust edit against the global config."""

    action: TrustAction
    workspace: str
    config_path: str
    trusted_workspaces: tuple[str, ...]
    #: Human-readable note when nothing changed (already trusted, or not on the list).
    note: str | None = None


def resolve_workspace(path: Path | str | None = None) -> Path:
    """Absolute directory to trust: ``path`` when given, else the process cwd."""
    target = Path.cwd() if path is None else Path(path)
    try:
        return target.expanduser().resolve()
    except OSError as exc:
        raise ConfigError(f"could not resolve workspace path {target}: {exc}") from exc


def read_global_trusted_workspaces(env: Mapping[str, str] | None = None) -> tuple[Path, list[str]]:
    """Return ``(global_config_path, trusted_workspaces)`` from the global TOML (empty list if absent).

    Raises:
        ConfigError: If the global file exists but is not valid TOML.
    """
    path = default_global_config_path(env)
    if not path.exists():
        return path, []
    data = _read_global(path)
    raw = data.get("trusted_workspaces", [])
    if raw is None:
        return path, []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"trusted_workspaces in {path} must be a list of strings")
    return path, list(raw)


def trust_workspace(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> TrustResult:
    """Add ``path`` (or cwd) to the global ``trusted_workspaces`` allowlist.

    Idempotent: a path already on the list (same resolved form) is left unchanged. Creates the global
    config file when it does not exist yet.
    """
    workspace = resolve_workspace(path)
    config_path, current = read_global_trusted_workspaces(env)
    workspace_key = str(workspace)
    if _already_listed(current, workspace):
        return TrustResult(
            action="unchanged",
            workspace=workspace_key,
            config_path=str(config_path),
            trusted_workspaces=tuple(_normalize_list(current)),
            note="workspace is already on the global trusted_workspaces allowlist",
        )
    updated = [*_normalize_list(current), workspace_key]
    _write_trusted_workspaces(config_path, updated)
    return TrustResult(
        action="added",
        workspace=workspace_key,
        config_path=str(config_path),
        trusted_workspaces=tuple(updated),
    )


def untrust_workspace(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> TrustResult:
    """Remove ``path`` (or cwd) from the global ``trusted_workspaces`` allowlist.

    Idempotent: a path not on the list leaves the file untouched (or creates nothing when absent).
    """
    workspace = resolve_workspace(path)
    config_path, current = read_global_trusted_workspaces(env)
    workspace_key = str(workspace)
    if not current:
        return TrustResult(
            action="missing",
            workspace=workspace_key,
            config_path=str(config_path),
            trusted_workspaces=(),
            note="global trusted_workspaces is empty; nothing to remove",
        )
    # * Compare against the NORMALIZED list, not the raw one. _normalize_list also de-duplicates, so
    # measuring against `current` reports "removed" (and rewrites the file) whenever the config merely
    # held a duplicate or an alias, for a path that was never on the list.
    normalized = _normalize_list(current)
    kept = [entry for entry in normalized if not _same_workspace(entry, workspace)]
    if len(kept) == len(normalized):
        return TrustResult(
            action="unchanged",
            workspace=workspace_key,
            config_path=str(config_path),
            trusted_workspaces=tuple(normalized),
            note="workspace is not on the global trusted_workspaces allowlist",
        )
    _write_trusted_workspaces(config_path, kept)
    return TrustResult(
        action="removed",
        workspace=workspace_key,
        config_path=str(config_path),
        trusted_workspaces=tuple(kept),
    )


def _read_global(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"global config at {path} is not valid TOML; fix it, then re-run trust/untrust: {exc}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"could not read global config at {path}: {exc}") from exc


def _already_listed(entries: Sequence[str], workspace: Path) -> bool:
    return any(_same_workspace(entry, workspace) for entry in entries)


def _same_workspace(entry: str, workspace: Path) -> bool:
    try:
        return Path(entry).expanduser().resolve() == workspace
    except OSError:
        return Path(entry) == workspace


def _normalize_list(entries: Sequence[str]) -> list[str]:
    """Resolve each entry to an absolute path string; keep unresolvable entries as-is."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            key = str(Path(entry).expanduser().resolve())
        except OSError:
            key = entry
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _write_trusted_workspaces(path: Path, workspaces: Sequence[str]) -> None:
    """Rewrite the global file's ``trusted_workspaces`` assignment; preserve the rest of the file."""
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"could not read global config at {path}: {exc}") from exc
        # * Re-parse so a race/corrupt mid-edit never silently compounds a bad file.
        _read_global(path)

    body = _strip_trusted_assignment(existing)
    try:
        assignment = _format_trusted_assignment(workspaces)
    except ValueError as exc:
        # * A path with no TOML representation is refused with NOTHING written. Reaching write_text
        # would truncate the global config first and leave zero bytes behind.
        raise ConfigError(
            f"cannot record this workspace in {path} ({exc}); trust a directory whose path is valid UTF-8"
        ) from exc
    # * Top-level keys must sit BEFORE any [table] header; appending after [agents.*] nests the
    # key under that table and the loader would never see a root trusted_workspaces.
    new_text = _insert_before_first_table(body, assignment)
    # * Verify IN MEMORY. write_text truncates its destination before it encodes, so a check that runs
    # after the write can only report damage that has already happened to the user's global config.
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write {path}: the rewritten config would not be valid TOML: {exc}") from exc
    written = parsed.get("trusted_workspaces", [])
    if list(written) != list(workspaces):
        raise ConfigError(
            f"refusing to write {path}: trusted_workspaces did not round-trip "
            f"(expected {list(workspaces)!r}, got {written!r})"
        )
    _atomic_write(path, new_text)


def _atomic_write(path: Path, new_text: str) -> None:
    """Write via a same-directory temp file + :func:`os.replace` so a reader never sees a half-written config.

    The global config is read by every server process on the machine, so a torn write is not a private
    failure. ``os.replace`` is atomic on POSIX and Windows when both paths share a filesystem, which a
    sibling temp file guarantees.
    """
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, handle_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        tmp = Path(handle_name)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        # * Carry the existing mode across: a fresh temp file takes the umask, which would silently widen
        # an owner-only config that can hold [agents.*.env] values.
        if path.exists():
            tmp.chmod(path.stat().st_mode & 0o7777)
        tmp.replace(path)  # Path.replace is os.replace: atomic within one filesystem
    except OSError as exc:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise ConfigError(f"could not write global config at {path}: {exc}") from exc


def _strip_trusted_assignment(text: str) -> str:
    """Drop a top-level ``trusted_workspaces = ...`` assignment (possibly multiline); keep comments.

    Only lines before the first ``[table]`` header are considered: a key after ``[agents.*]`` is not
    the root allowlist and must not be stripped (and trust never writes one there).
    """
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    in_table = False
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if not in_table and stripped.startswith("[") and not stripped.startswith("#"):
            in_table = True
        if not in_table and stripped.startswith(_TRUST_HEADER_PREFIX):
            # * Drop OUR OWN managed header wherever it sits above the assignment. It is re-emitted by
            # _format_trusted_assignment on every write, so keeping it here appended a fresh copy each
            # time and the file grew a header per trust/untrust. Matching on the stable prefix rather
            # than the exact line also collapses copies left by an earlier wording.
            i += 1
            continue
        if in_table or stripped.startswith("#") or not _ASSIGNMENT.match(line):
            out.append(line)
            i += 1
            continue
        balance = _bracket_delta(line)
        i += 1
        while balance > 0 and i < len(lines):
            balance += _bracket_delta(lines[i])
            i += 1
        if balance > 0:
            # * Fail BEFORE anything is written: running off the end means the array never closed, and
            # continuing would silently swallow every line below it.
            raise ConfigError(
                "the trusted_workspaces assignment in the global config has an unterminated array; "
                "fix it by hand, then re-run trust/untrust"
            )
        # * Drop a trailing blank line left behind by the removed block so re-inserts stay tidy.
        if out and out[-1].strip() == "":
            out.pop()
    return "".join(out)


def _bracket_delta(line: str) -> int:
    """Net ``[`` minus ``]`` counting only brackets OUTSIDE strings and comments.

    A raw ``line.count("[")`` also counts a bracket inside a quoted path -- and ``[`` / ``]`` are legal
    directory characters on every platform Rutherford supports -- or inside a trailing comment. Either
    walks the multiline-array scan past the real end of the assignment, so every following line is
    dropped: the user's ``[agents.*]`` tables are deleted while the write still reports success and the
    round-trip check still passes, because ``trusted_workspaces`` itself round-trips perfectly.

    LIMITATION: this is a per-line scanner with no cross-line string state, so a MULTI-LINE TOML string
    (``\"\"\"..\"\"\"`` / ``'''..'''``) holding a bracket on a continuation line is miscounted. Rutherford never
    writes that form -- it would take a hand-edited config with a newline inside a directory name -- and
    the outcome is fail-safe either way: the balance guard or the in-memory round-trip check refuses the
    edit and the config is left untouched. Never silent corruption.
    """
    delta = 0
    index = 0
    length = len(line)
    while index < length:
        ch = line[index]
        if ch == "#":
            break  # a comment runs to end of line; brackets in it are not structure
        if ch == '"':
            index += 1
            while index < length:
                if line[index] == "\\":
                    index += 2  # skip an escaped char so \" does not end the string early
                    continue
                if line[index] == '"':
                    break
                index += 1
        elif ch == "'":
            index += 1  # a TOML literal string has no escapes
            while index < length and line[index] != "'":
                index += 1
        elif ch == "[":
            delta += 1
        elif ch == "]":
            delta -= 1
        index += 1
    return delta


def _insert_before_first_table(body: str, assignment: str) -> str:
    """Insert ``assignment`` before the first TOML table header, or append when none exist."""
    if not body.strip():
        return assignment
    lines = body.splitlines(keepends=True)
    insert_at = len(lines)
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("[") and not stripped.startswith("#"):
            insert_at = index
            break
    before = "".join(lines[:insert_at])
    after = "".join(lines[insert_at:])
    if before and not before.endswith("\n"):
        before += "\n"
    if before and not before.endswith("\n\n"):
        before += "\n"
    if after and not assignment.endswith("\n"):
        assignment += "\n"
    if after and not after.startswith("\n") and assignment.endswith("\n"):
        # * Keep a blank line between the allowlist block and the first [table].
        return before + assignment + "\n" + after
    return before + assignment + after


def _format_trusted_assignment(workspaces: Sequence[str]) -> str:
    """Render ``trusted_workspaces`` as TOML (empty array when the allowlist is cleared).

    Raises:
        ValueError: Via :func:`~rutherford.io.tomltext.toml_str`, for a path with no TOML
            representation at all (a lone surrogate). Raised here, before the caller opens anything.
    """
    if not workspaces:
        return f"{_TRUST_HEADER}trusted_workspaces = []\n"
    items = ",\n".join(f"    {toml_str(item)}" for item in workspaces)
    return f"{_TRUST_HEADER}trusted_workspaces = [\n{items},\n]\n"
