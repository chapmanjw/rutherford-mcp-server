# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Global trusted-workspace allowlist: add/remove a path in the platform ``config.toml``.

The write/yolo gate reads ``trusted_workspaces`` from the merged config. These helpers edit the
*global* file only (``default_global_config_path``), so a one-shot ``rutherford trust`` from a repo
root registers that directory for every server process that loads the global config. A project-local
``trusted_workspaces`` still replaces (does not union) the global list at load time -- see
:func:`~rutherford.config.loader.deep_merge`.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..domain.errors import ConfigError
from .loader import default_global_config_path

#: Action reported by :func:`trust_workspace` / :func:`untrust_workspace`.
TrustAction = Literal["added", "removed", "unchanged", "missing"]

_ASSIGNMENT = re.compile(r"^(\s*)trusted_workspaces\s*=", re.MULTILINE)

_TRUST_HEADER = (
    "# Absolute paths under which write/yolo delegations are permitted "
    "(managed by `rutherford trust` / `rutherford untrust`).\n"
)


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
    kept = [entry for entry in _normalize_list(current) if not _same_workspace(entry, workspace)]
    if len(kept) == len(current):
        return TrustResult(
            action="unchanged",
            workspace=workspace_key,
            config_path=str(config_path),
            trusted_workspaces=tuple(_normalize_list(current)),
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
    assignment = _format_trusted_assignment(workspaces)
    # * Top-level keys must sit BEFORE any [table] header; appending after [agents.*] nests the
    # key under that table and the loader would never see a root trusted_workspaces.
    new_text = _insert_before_first_table(body, assignment)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not write global config at {path}: {exc}") from exc
    # * Round-trip check: the file must still parse and carry the intended list.
    written = _read_global(path).get("trusted_workspaces", [])
    if list(written) != list(workspaces):
        raise ConfigError(
            f"wrote {path} but trusted_workspaces did not round-trip (expected {list(workspaces)!r}, got {written!r})"
        )


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
        if in_table or stripped.startswith("#") or not _ASSIGNMENT.match(line):
            out.append(line)
            i += 1
            continue
        balance = line.count("[") - line.count("]")
        i += 1
        while balance > 0 and i < len(lines):
            nxt = lines[i]
            balance += nxt.count("[") - nxt.count("]")
            i += 1
        # * Drop a trailing blank line left behind by the removed block so re-inserts stay tidy.
        if out and out[-1].strip() == "":
            out.pop()
    return "".join(out)


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
    """Render ``trusted_workspaces`` as TOML (empty array when the allowlist is cleared)."""
    if not workspaces:
        return f"{_TRUST_HEADER}trusted_workspaces = []\n"
    items = ",\n".join(f"    {_toml_str(item)}" for item in workspaces)
    return f"{_TRUST_HEADER}trusted_workspaces = [\n{items},\n]\n"


def _toml_str(value: str) -> str:
    """Quote ``value`` as a TOML basic string (Windows paths need backslash escapes)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
