# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The write/propose sandbox: run a mutating ACP turn in an isolated execution root, never the user's tree.

ACP permission control alone is not containment -- the agent's own OS process can touch the filesystem
outside what it routes through the client, and even a routed ``fs/write`` lands wherever the agent points
it. So a MUTATING delegation (``write`` / ``propose`` / ``yolo``) does not run in the user's ``working_dir``;
it runs in an isolated root this module builds, and the user's tree is only touched -- and only for
``write`` / ``yolo`` -- by applying back the reviewed diff.

Two isolation strategies, chosen by whether ``working_dir`` is a git repo:

* **git repo** -- an ephemeral detached git worktree off the repo's current ``HEAD``
  (``git worktree add --detach``). The agent edits the worktree; the diff is computed from it
  (``git add -A`` then ``git diff --cached --binary`` for a full, binary-safe patch plus the changed-path
  list). For ``propose`` the worktree is discarded and nothing is applied; for ``write`` / ``yolo`` the
  patch is applied back to the real repo (``git apply``) and *then* the worktree removed.
* **not a git repo** -- a temporary COPY of the directory (bounded by a size guard so a huge tree is
  refused rather than silently copied). The agent edits the copy; the changed/created files are diffed
  against the original and, for ``write`` / ``yolo``, copied back. Over the guard, the run is refused with a
  clear ``WORKSPACE_NOT_TRUSTED``-adjacent error telling the caller write mode needs a git working_dir.

The execution root is always cleaned up in a ``finally`` (worktree removed, temp copy deleted), and the
agent's process tree is reaped by the existing session teardown. ``SandboxResult`` carries the unified diff
and the changed-file list back to the delegation service, which stamps them onto the
:class:`~rutherford.domain.models.DelegationResult`.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.enums import SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError

_log = logging.getLogger("rutherford.acp.sandbox")

#: How long any single git/copy helper may run before it is judged hung. Generous -- a worktree add or an
#: apply on a large repo is still seconds, not minutes -- but bounded so a wedged git can never stall a turn.
_GIT_TIMEOUT_S = 60.0

#: The size ceiling (bytes) for the non-git temp-copy fallback. A working_dir whose tracked content exceeds
#: this is refused for a mutating run rather than copied -- the copy would be slow and the diff-back
#: unreliable, and the right answer there is a git working_dir. 256 MiB covers an ordinary source tree.
_MAX_COPY_BYTES = 256 * 1024 * 1024

#: Directory names never copied into the non-git temp sandbox (and never diffed back): heavyweight,
#: regenerable, or Rutherford's own bookkeeping. Keeps the copy fast and the diff focused on real edits.
_COPY_EXCLUDES: frozenset[str] = frozenset(
    {".git", ".rutherford", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
)


@dataclass(slots=True)
class SandboxResult:
    """The outcome of a sandboxed mutating turn: what changed and (for write/yolo) whether it was applied.

    ``diff`` is the unified patch the agent produced (empty when it changed nothing); ``changed_files`` is the
    repo-relative path of every file it created or edited; ``applied`` is ``True`` when the patch was applied
    back to the real ``working_dir`` (``write`` / ``yolo``) and ``False`` for ``propose`` (nothing applied) or
    an empty change set.
    """

    diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    applied: bool = False


class Sandbox:
    """A live isolated execution root for one mutating turn: its ``root`` is the agent's confined cwd.

    Built by :meth:`SandboxManager.open`; used as an (async-free) context-managed handle whose ``root`` is
    handed to the spawn cwd, the ACP ``session/new`` cwd, and the :class:`~rutherford.acp.client` file/terminal
    confinement. After the turn the service calls :meth:`finish` (compute the diff, apply it for write/yolo)
    and then :meth:`cleanup` in a ``finally`` (always). The user's ``working_dir`` is never the agent's cwd.
    """

    def __init__(self, *, manager: SandboxManager, working_dir: Path, root: Path, is_git: bool) -> None:
        self._manager = manager
        self._working_dir = working_dir
        self._root = root
        self._is_git = is_git
        self._cleaned = False

    @property
    def root(self) -> str:
        """The agent's confined execution root (the worktree or the temp copy), as an absolute string."""
        return str(self._root)

    def finish(self, mode: SafetyMode) -> SandboxResult:
        """Compute the changed set, and for ``write`` / ``yolo`` apply it back to the real ``working_dir``.

        For ``propose`` the diff is computed and returned but nothing is applied -- the real tree is untouched.
        For ``write`` / ``yolo`` the patch (git repo) or the changed files (non-git copy) are written back to
        ``working_dir``. Never raises for "the agent changed nothing"; that is an empty :class:`SandboxResult`.
        """
        if self._is_git:
            diff, changed, deleted = self._git_changes()
        else:
            diff, changed, deleted = self._copy_changes()
        all_changed = sorted({*changed, *deleted})
        if not all_changed:
            return SandboxResult()
        apply = mode in (SafetyMode.WRITE, SafetyMode.YOLO)
        if apply:
            # Apply by COPYING the changed files byte-for-byte from the sandbox (and removing the deleted
            # ones), not via ``git apply``: a patch round-trip is at the mercy of the repo's line-ending
            # normalization (``core.autocrlf`` on Windows injects ``\r`` into the applied file), so copying is
            # the reliable, byte-faithful apply. The git diff above is still the returned ``diff`` / patch.
            self._copy_apply(changed)
            self._delete_back(deleted)
        return SandboxResult(diff=diff, changed_files=all_changed, applied=apply)

    def cleanup(self) -> None:
        """Remove the execution root (worktree or temp copy). Idempotent, best-effort, never raises."""
        if self._cleaned:
            return
        self._cleaned = True
        if self._is_git:
            self._manager.remove_worktree(self._working_dir, self._root)
        else:
            shutil.rmtree(self._root, ignore_errors=True)

    # --- git strategy --------------------------------------------------------

    def _git_changes(self) -> tuple[str, list[str], list[str]]:
        """Stage the worktree, then read the binary patch plus the created/edited and deleted path lists.

        Returns ``(diff, changed, deleted)``: the full ``git diff --cached --binary`` patch, the paths that
        were added or modified (copied back on apply), and the paths that were deleted (removed on apply).
        ``--name-status`` distinguishes a delete (``D``) so an agent that removed a file is honoured, not just
        an agent that added one.
        """
        self._manager.run_git(self._root, "add", "-A")
        diff = self._manager.run_git(self._root, "diff", "--cached", "--binary")
        status = self._manager.run_git(self._root, "diff", "--cached", "--name-status")
        changed: list[str] = []
        deleted: list[str] = []
        for line in status.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, path = parts[0].strip(), parts[-1].strip()
            if code.startswith("D"):
                deleted.append(path)
            else:  # A (add), M (modify), R (rename target), C (copy): all land as a copy of the new path
                changed.append(path)
        return diff, changed, deleted

    def _delete_back(self, deleted: list[str]) -> None:
        """Remove from the real ``working_dir`` each file the agent deleted in the sandbox (write/yolo apply)."""
        for rel in deleted:
            target = self._contained_target(rel)
            if target is not None and target.is_file():
                target.unlink()

    def _contained_target(self, rel: str) -> Path | None:
        """The real-tree path for ``rel``, or ``None`` if it would escape ``working_dir`` (symlink guard).

        Apply-back and delete-back resolve ``working_dir / rel`` and refuse it unless it stays within the
        resolved ``working_dir``. Without this a symlink inside the workspace (e.g. ``link -> /etc``) would let
        an edit to ``link/x`` follow the symlink and overwrite a file OUTSIDE the tree the user trusted -- a
        write escape. A path that resolves outside is skipped (logged), never written.
        """
        base = self._working_dir.resolve()
        try:
            resolved = (self._working_dir / rel).resolve()
        except OSError:
            return None
        if resolved == base or resolved.is_relative_to(base):
            return self._working_dir / rel
        _log.warning("sandbox apply-back skipped %s: it resolves outside the working_dir (symlink escape)", rel)
        return None

    # --- non-git temp-copy strategy -----------------------------------------

    def _copy_changes(self) -> tuple[str, list[str], list[str]]:
        """Diff the temp copy against the original: created/edited files, DELETED files, and a text diff.

        Returns ``(diff, changed, deleted)``. ``changed`` is every file in the copy whose bytes differ from
        (or are new vs) the original; ``deleted`` is every original file (minus the excluded dirs) that is GONE
        from the copy -- so a write/yolo agent that removed a file in the sandbox has that deletion applied back,
        matching the git path (previously the non-git path lost deletions entirely). The diff is best-effort
        text (binary files are listed but rendered as a one-line marker), since this path has no git to produce
        a binary patch; the authoritative output is the changed/deleted lists the apply-back uses.
        """
        changed: list[str] = []
        diff_parts: list[str] = []
        in_copy: set[str] = set()
        for path in _walk_files(self._root):
            rel = path.relative_to(self._root)
            in_copy.add(rel.as_posix())
            original = self._working_dir / rel
            new_bytes = path.read_bytes()
            if original.is_file() and original.read_bytes() == new_bytes:
                continue
            changed.append(rel.as_posix())
            diff_parts.append(_text_file_diff(rel.as_posix(), original, new_bytes))
        deleted: list[str] = []
        for path in _walk_files(self._working_dir):
            original_rel = path.relative_to(self._working_dir).as_posix()
            if original_rel not in in_copy:
                deleted.append(original_rel)
                diff_parts.append(f"--- a/{original_rel}\n+++ /dev/null\n(file deleted)")
        return "\n".join(diff_parts), sorted(changed), sorted(deleted)

    def _copy_apply(self, changed: list[str]) -> None:
        """Copy each changed/created file from the temp copy back into the real ``working_dir`` (contained)."""
        for rel in changed:
            dst = self._contained_target(rel)
            if dst is None:
                continue  # would escape the working_dir via a symlink -- refused
            src = self._root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


class SandboxManager:
    """Builds and tears down the isolated execution root for a mutating delegation.

    The orchestration-side coordinator: it decides git-worktree vs temp-copy, shells out to git (via
    ``subprocess``, no new dependency), and enforces the non-git size guard. Stateless across calls -- each
    :meth:`open` returns a fresh :class:`Sandbox` the caller drives and cleans up.
    """

    def open(self, working_dir: str) -> Sandbox:
        """Create an isolated execution root for ``working_dir`` and return the :class:`Sandbox` handle.

        A git ``working_dir`` gets a detached worktree off its current ``HEAD``; a non-git one gets a bounded
        temp copy (refused over the size guard with a clear error). Raises :class:`RutherfordError` only for a
        hard setup failure (git unavailable mid-build, the copy guard tripped); a normal "nothing changed"
        turn is handled later in :meth:`Sandbox.finish`.
        """
        resolved = Path(working_dir).resolve()
        if self._is_git_repo(resolved):
            root = self._add_worktree(resolved)
            return Sandbox(manager=self, working_dir=resolved, root=root, is_git=True)
        root = self._copy_tree(resolved)
        return Sandbox(manager=self, working_dir=resolved, root=root, is_git=False)

    # --- git plumbing --------------------------------------------------------

    def _is_git_repo(self, working_dir: Path) -> bool:
        """Whether ``working_dir`` is a git work tree WITH a commit (so a detached worktree can be added).

        Requires both ``--is-inside-work-tree`` and a resolvable ``HEAD``: a freshly ``git init``-ed repo with
        no commit has no ``HEAD`` and cannot host a worktree, so it is treated as non-git and falls to the
        temp-copy strategy rather than failing the run.
        """
        try:
            inside = self._git(working_dir, "rev-parse", "--is-inside-work-tree")
        except (RutherfordError, OSError):
            return False
        if inside.strip() != "true":
            return False
        try:
            self._git(working_dir, "rev-parse", "--verify", "HEAD")
        except (RutherfordError, OSError):
            return False  # a repo with no commit yet -- use the copy strategy
        return True

    def _add_worktree(self, working_dir: Path) -> Path:
        """Create a detached worktree off ``HEAD`` in a fresh temp dir and return its path.

        Detached so the worktree carries no branch of its own (it is thrown away), off ``HEAD`` so the agent
        starts from the repo's current committed state. A repo with no commits yet (no ``HEAD``) cannot host a
        worktree, so it falls back to the temp-copy strategy -- surfaced by raising, which :meth:`open` does
        not catch, so the caller learns write mode needs a committed git tree or a copyable dir.
        """
        temp = Path(tempfile.mkdtemp(prefix="rutherford-sbx-"))
        worktree = temp / "wt"
        try:
            self._git(working_dir, "worktree", "add", "--detach", str(worktree), "HEAD")
        except RutherfordError:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        return worktree

    def remove_worktree(self, working_dir: Path, worktree: Path) -> None:
        """Remove a worktree (``git worktree remove --force``) and delete its temp parent. Best-effort."""
        try:
            self._git(working_dir, "worktree", "remove", "--force", str(worktree))
        except (RutherfordError, OSError) as exc:  # the dir cleanup below is the backstop
            _log.warning("git worktree remove failed for %s: %s", worktree, exc)
        shutil.rmtree(worktree.parent, ignore_errors=True)
        # Prune the repo's stale worktree administrative entries so a failed remove leaves no dangling ref.
        with contextlib.suppress(RutherfordError, OSError):
            self._git(working_dir, "worktree", "prune")

    def run_git(self, cwd: Path, *args: str) -> str:
        """Run a git subcommand in ``cwd`` and return its stdout (raising :class:`RutherfordError` on failure)."""
        return self._git(cwd, *args)

    def _git(self, cwd: Path, *args: str) -> str:
        """Run ``git -C <cwd> <args>`` and return stdout; raise :class:`RutherfordError` on a non-zero exit.

        The single git entry point -- one ``subprocess.run`` with a hard timeout, text I/O, and no shell. A
        non-zero exit or a launch failure (git missing) becomes a typed error the caller maps to an envelope,
        never a silent partial sandbox. ``core.autocrlf=false`` is forced so the staged diff stays
        byte-faithful on Windows (the worktree blob is not line-ending-normalized), matching the byte-for-byte
        file copy used to apply the changes back.
        """
        cmd = ["git", "-c", "core.autocrlf=false", "-C", str(cwd), *args]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RutherfordError(ErrorCode.INTERNAL, "git is not installed; the write sandbox needs git") from exc
        except subprocess.TimeoutExpired as exc:
            msg = f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S:.0f}s"
            raise RutherfordError(ErrorCode.INTERNAL, msg) from exc
        if completed.returncode != 0:
            raise RutherfordError(
                ErrorCode.INTERNAL,
                f"git {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()}",
            )
        return completed.stdout

    # --- non-git temp-copy plumbing -----------------------------------------

    def _copy_tree(self, working_dir: Path) -> Path:
        """Copy ``working_dir`` (minus the excluded dirs) into a temp dir, enforcing the size guard first.

        Refuses a tree whose copyable content exceeds :data:`_MAX_COPY_BYTES` with a clear error -- write mode
        on a huge non-git dir should use a git working_dir, not a slow, unreliable copy. The excluded dirs
        (``.git`` is absent here by definition, plus ``node_modules`` / virtualenvs / caches) are skipped so
        the copy is the source, not its regenerable artifacts.
        """
        total = _tree_size(working_dir)
        if total > _MAX_COPY_BYTES:
            raise RutherfordError(
                ErrorCode.WORKSPACE_NOT_TRUSTED,
                f"write mode needs a git working_dir: the non-git directory is {total // (1024 * 1024)} MiB, over the "
                f"{_MAX_COPY_BYTES // (1024 * 1024)} MiB copy-sandbox guard. Run `git init` and commit, or point at a "
                "smaller directory.",
            )
        temp = Path(tempfile.mkdtemp(prefix="rutherford-sbx-"))
        copy = temp / "copy"
        shutil.copytree(working_dir, copy, ignore=shutil.ignore_patterns(*_COPY_EXCLUDES), dirs_exist_ok=True)
        return copy


def _walk_files(root: Path) -> list[Path]:
    """Every regular file under ``root``, skipping the excluded dirs, sorted for a deterministic diff."""
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in _COPY_EXCLUDES for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def _tree_size(root: Path) -> int:
    """The total byte size of ``root``'s files, skipping the excluded dirs (the copy guard's measurement)."""
    total = 0
    for path in _walk_files(root):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _text_file_diff(rel: str, original: Path, new_bytes: bytes) -> str:
    """A short unified-diff-style block for one changed file in the non-git path (text where possible)."""
    try:
        new_text = new_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return f"--- a/{rel}\n+++ b/{rel}\n(binary file changed)"
    old_text = ""
    if original.is_file():
        try:
            old_text = original.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            old_text = ""
    lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    return "".join(lines).rstrip("\n")
