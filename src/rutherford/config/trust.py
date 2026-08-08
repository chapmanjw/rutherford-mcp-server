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

import contextlib
import os
import re
import tempfile
import time
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

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
    workspace_key = str(workspace)
    # * The lock spans the READ as well as the write. Reading outside it would let a concurrent edit
    # land between the two, and this call would then write an allowlist computed from a stale one.
    with _config_lock(default_global_config_path(env)):
        config_path, current = read_global_trusted_workspaces(env)
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
    workspace_key = str(workspace)
    # * Held across read and write: a revocation racing an unrelated trust must not be undone by that
    # trust writing back a list it read before this removal. That direction fails OPEN.
    with _config_lock(default_global_config_path(env)):
        config_path, current = read_global_trusted_workspaces(env)
        if not current:
            return TrustResult(
                action="missing",
                workspace=workspace_key,
                config_path=str(config_path),
                trusted_workspaces=(),
                note="global trusted_workspaces is empty; nothing to remove",
            )
        # * Compare against the NORMALIZED list, not the raw one. _normalize_list also de-duplicates, so
        # measuring against `current` reports "removed" (and rewrites the file) whenever the config
        # merely held a duplicate or an alias, for a path that was never on the list.
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


#: How long to wait for another process to finish its edit before giving up.
#:
#: There is deliberately no automatic stale-lock breaking. Age cannot establish that a lock is abandoned
#: -- only that its holder is slow -- and breaking on age reintroduces exactly the race the lock exists
#: to close: two waiters both judge a lock stale, the slower one removes the FRESH lock the faster one
#: just took, and both enter the critical section. A leftover lock after a crash is recoverable with one
#: delete, and the timeout message names the file; a silent double-acquire on a security allowlist is not
#: recoverable at all, because nobody finds out.
_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.05


@contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    """Serialize the whole read-modify-write on ``path`` across processes.

    The atomic replace in :func:`_atomic_write` prevents a torn READ; it does nothing about a lost
    UPDATE. Two edits that both read the same allowlist and then both write produce whichever ran last,
    and an interleaved trust + untrust can put back an entry the user just removed -- a security
    allowlist failing OPEN, which is the half worth closing.

    Uses an ``O_EXCL`` lock file rather than ``fcntl``/``msvcrt`` so one code path covers POSIX and
    Windows with no dependency. Advisory: it coordinates Rutherford with itself, not with a text editor.
    """
    lock = path.with_name(path.name + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"could not prepare the config directory at {lock.parent}: {exc}") from exc

    token = f"{os.getpid()}:{uuid4().hex}"
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"another process is editing {path}. If nothing else is running, that edit was "
                    f"interrupted -- delete {lock} and try again."
                ) from None
            time.sleep(_LOCK_POLL_S)
        except OSError as exc:
            raise ConfigError(f"could not lock {path} for editing: {exc}") from exc

    try:
        with contextlib.suppress(OSError):
            os.write(handle, token.encode("ascii"))
        os.close(handle)  # * close before yielding so the release path only has to unlink
        yield
    finally:
        _release_lock(lock, token)


def _release_lock(lock: Path, token: str) -> None:
    """Drop the lock only if it is still OURS.

    An unconditional unlink is ownership-blind: an edit that overran the stale timeout would otherwise
    delete whichever successor now holds the path. Leaving a foreign lock in place is the safe error --
    it ages out on its own.
    """
    with contextlib.suppress(OSError):
        if lock.read_text(encoding="utf-8") == token:
            lock.unlink()


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
    multiline: str | None = None
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if multiline is not None:
            # * Still inside a multi-line string opened on an earlier line. Pass the line through and
            # only advance the string state: its content is not structure, so a line beginning "[" here
            # is NOT a table header and a bracket in it is NOT array nesting.
            _, multiline = _scan_brackets(line, multiline)
            out.append(line)
            i += 1
            continue
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
            # * Track a multi-line string that OPENS on a passed-through line, so the lines it spans are
            # recognized as string content rather than structure.
            _, multiline = _scan_brackets(line)
            out.append(line)
            i += 1
            continue
        balance, multiline = _scan_brackets(line)
        i += 1
        while (balance > 0 or multiline is not None) and i < len(lines):
            step, multiline = _scan_brackets(lines[i], multiline)
            balance += step
            i += 1
        if balance > 0 or multiline is not None:
            # * Fail BEFORE anything is written: running off the end means the array (or a string inside
            # it) never closed, and continuing would silently swallow every line below it.
            raise ConfigError(
                "the trusted_workspaces assignment in the global config has an unterminated array; "
                "fix it by hand, then re-run trust/untrust"
            )
        # * Drop a trailing blank line left behind by the removed block so re-inserts stay tidy.
        if out and out[-1].strip() == "":
            out.pop()
    return "".join(out)


def _find_multiline_close(line: str, delim: str, start: int) -> int:
    """Index just PAST the closing ``delim``, or ``-1`` when this line does not close it.

    A plain ``str.find`` is wrong for a multi-line BASIC string, because a backslash-escaped quote is
    content rather than part of the terminator. Treating one as a terminator ends the string early, and
    every bracket after it -- still string content -- then counts as array structure. Multi-line LITERAL
    strings have no escapes, so a direct find is correct for those.

    Known limitation: a backslash as the final character of a line (TOML's line-ending continuation) is
    not carried into the next line's scan. It cannot arise from anything Rutherford writes, and the
    in-memory validation refuses the result rather than writing it.
    """
    if delim == "'''":
        found = line.find(delim, start)
        return -1 if found < 0 else found + 3
    index = start
    while index < len(line):
        if line[index] == "\\":
            index += 2  # an escaped char, whatever it is, cannot start the terminator
            continue
        if line.startswith(delim, index):
            return index + 3
        index += 1
    return -1


def _scan_brackets(line: str, in_multiline: str | None = None) -> tuple[int, str | None]:  # noqa: C901 - a scanner
    """Net ``[`` minus ``]`` over one line, counting only brackets that are real array structure.

    Returns ``(delta, still_open)``, where ``still_open`` is the delimiter of a multi-line string this
    line left unterminated (``\"\"\"`` or ``'''``) or ``None``. Feed it back on the next line: string
    content spans lines, so the state has to as well.

    Everything skipped here is skipped because a raw ``line.count("[")`` gets it wrong:

    * A bracket inside a quoted path. ``[`` and ``]`` are legal directory characters on every platform
      Rutherford supports, and ``trust`` defaults to the current directory.
    * A bracket in a trailing comment.
    * A bracket inside a multi-line string, on any of the lines it spans.

    Miscounting any of them walks the array scan past the real end of the assignment, and every line
    below it is then dropped -- deleting the user's ``[agents.*]`` tables while the write still reports
    success, because ``trusted_workspaces`` itself round-trips perfectly and the check only inspects it.
    """
    delta = 0
    index = 0
    length = len(line)

    if in_multiline is not None:
        close = _find_multiline_close(line, in_multiline, 0)
        if close < 0:
            return 0, in_multiline  # the whole line is string content
        index = close
        in_multiline = None

    while index < length:
        ch = line[index]
        if ch == "#":
            break  # a comment runs to end of line
        if line.startswith('"""', index) or line.startswith("'''", index):
            delim = line[index : index + 3]
            close = _find_multiline_close(line, delim, index + 3)
            if close < 0:
                return delta, delim  # opens here, continues past this line
            index = close
            continue
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
    return delta, in_multiline


def _insert_before_first_table(body: str, assignment: str) -> str:
    """Insert ``assignment`` before the first TOML table header, or append when none exist.

    "First table header" has to mean a real one. A line beginning ``[`` inside a multi-line string is
    string content, and inserting there would splice the assignment INTO that string -- so the key never
    lands at the top level and the caller's round-trip check refuses the write.
    """
    if not body.strip():
        return assignment
    lines = body.splitlines(keepends=True)
    insert_at = len(lines)
    multiline: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if multiline is None and stripped.startswith("[") and not stripped.startswith("#"):
            insert_at = index
            break
        _, multiline = _scan_brackets(line, multiline)
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
