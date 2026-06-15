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

Two limitations are deliberate, given the threat model (orchestrating COOPERATIVE coding agents the user
chose to run on their own machine, not sandboxing adversarial code):

* **Not an OS jail.** The isolation is cwd + the ACP file/terminal path-escape guard. A write/yolo agent's
  own OS process, or a terminal command it runs, can still write an absolute path outside the sandbox. Full
  OS containment (Windows Job Objects / ACLs) is deferred. This is strictly safer than v2, which ran agents
  directly in the user's tree with no sandbox at all.
* **A narrow apply-time TOCTOU.** The clobber / concurrent-edit checks run, then the changed files are copied
  back. A user save to one of those files in the sub-millisecond window between the check and the copy is not
  detected. Eliminating it would need to lock the whole working tree for the apply; the window is tiny and a
  user actively editing files the agent is mid-write on is outside the cooperative model, so it is accepted
  (the same inherent gap any check-then-write filesystem apply has, ``git apply`` / ``git stash`` included).
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
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

    def __init__(
        self,
        *,
        manager: SandboxManager,
        working_dir: Path,
        root: Path,
        is_git: bool,
        baseline: dict[str, str] | None = None,
    ) -> None:
        self._manager = manager
        self._working_dir = working_dir
        self._root = root
        self._is_git = is_git
        #: For the non-git temp-copy path: ``{relpath: sha256}`` of the working_dir AS IT WAS at open time, so
        #: change detection is agent-relative (a file the agent edited, not one that merely differs from the
        #: live tree) and a concurrent user edit during the turn can be detected and refused at apply. ``None``
        #: for the git path (which diffs the worktree against HEAD instead).
        self._baseline = baseline if baseline is not None else {}
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
            # Refuse to clobber the user's work before touching the real tree. Git: a worktree is off HEAD, so a
            # changed file is HEAD + the agent's edit, NOT any uncommitted edit the user has -- refuse if a
            # touched path is dirty vs HEAD. Non-git: the temp copy is a snapshot at open time, so refuse if a
            # touched path changed in the real tree DURING the turn (a concurrent edit the apply would lose).
            if self._is_git:
                self._refuse_if_clobbering([*changed, *deleted])
            else:
                self._refuse_if_concurrently_edited([*changed, *deleted])
            # Apply by COPYING the changed files byte-for-byte from the sandbox (and removing the deleted
            # ones), not via ``git apply``: a patch round-trip is at the mercy of the repo's line-ending
            # normalization (``core.autocrlf`` on Windows injects ``\r`` into the applied file), so copying is
            # the reliable, byte-faithful apply. The git diff above is still the returned ``diff`` / patch.
            self._copy_apply(changed)
            self._delete_back(deleted)
        return SandboxResult(diff=diff, changed_files=all_changed, applied=apply)

    def _refuse_if_concurrently_edited(self, paths: list[str]) -> None:
        """Refuse a non-git apply if a touched path changed in the real tree since the sandbox opened.

        The temp copy is a snapshot of ``working_dir`` at open time (the baseline). If the user edited (or
        created, or deleted) one of the files the apply would write or delete WHILE the agent was running, the
        real file no longer matches the baseline -- applying the sandbox version would silently overwrite that
        concurrent edit. So the whole apply is refused with a clear, actionable error rather than racing.
        """
        conflicts = [rel for rel in paths if self._real_hash(rel) != self._baseline.get(rel)]
        if conflicts:
            raise RutherfordError(
                ErrorCode.WORKSPACE_NOT_TRUSTED,
                "the working directory changed under the delegation ("
                f"{', '.join(sorted(conflicts))}); the apply was refused to avoid overwriting a concurrent "
                "edit. Retry with the directory quiescent.",
            )

    def _real_hash(self, rel: str) -> str | None:
        """The sha256 of ``working_dir/rel`` now, or ``None`` if it does not exist (the non-git baseline key)."""
        target = self._working_dir / rel
        if not target.is_file():
            return None
        try:
            return _sha256(target.read_bytes())
        except OSError:
            return None

    def _refuse_if_clobbering(self, paths: list[str]) -> None:
        """Refuse a git apply-back that would overwrite or delete an UNCOMMITTED local edit (worktree is off HEAD).

        If any path the apply would touch is dirty (modified / staged / untracked) vs ``HEAD`` in the real
        ``working_dir``, the apply would silently replace the user's local work with the worktree's HEAD-based
        version, so the whole apply is refused with a clear, actionable error. A path the user does not have
        locally (e.g. a brand-new file the agent created) is not dirty and never blocks.
        """
        conflicts = [rel for rel in paths if self._is_dirty_vs_head(rel)]
        if conflicts:
            raise RutherfordError(
                ErrorCode.WORKSPACE_NOT_TRUSTED,
                "the working tree has uncommitted changes to "
                f"{', '.join(sorted(conflicts))}. A write delegation runs from HEAD in an isolated worktree, so "
                "applying it back would overwrite those local edits. Commit or stash them first, then retry.",
            )

    def _is_dirty_vs_head(self, rel: str) -> bool:
        """Whether ``rel`` is modified / staged / untracked in the real ``working_dir`` (non-empty porcelain).

        Uses the repo's OWN autocrlf policy (not the forced-off one the sandbox diff uses): otherwise a file
        git checked out with CRLF under ``autocrlf=true`` would read as modified and wrongly block the apply.
        """
        return bool(self._manager.run_git_user_config(self._working_dir, "status", "--porcelain", "--", rel).strip())

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
        an agent that added one. ``--no-renames`` forces a rename to surface as a delete + an add (so the OLD
        path is removed and the NEW one copied) regardless of the repo's ``diff.renames`` config -- otherwise a
        user with rename detection on would get an ``R old new`` line whose old path was silently left behind.
        """
        self._manager.run_git(self._root, "add", "-A")
        diff = self._manager.run_git(self._root, "diff", "--cached", "--binary", "--no-renames")
        status = self._manager.run_git(self._root, "diff", "--cached", "--name-status", "--no-renames")
        changed: list[str] = []
        deleted: list[str] = []
        for line in status.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, path = parts[0].strip(), parts[-1].strip()
            if code.startswith("D"):
                deleted.append(path)
            else:  # A (add), M (modify): land as a copy of the new path (--no-renames removes the R/C cases)
                changed.append(path)
        return diff, changed, deleted

    def _delete_back(self, deleted: list[str]) -> None:
        """Remove from the real ``working_dir`` each entry the agent deleted in the sandbox (write/yolo apply).

        Uses the PARENT-resolved guard, not the fully-resolved one: a deleted entry that is itself a symlink is
        removed as the LINK (``unlink`` never follows it), so deleting a workspace symlink that points outside
        removes the link and not its external target -- while a path whose parent dir escapes the working_dir is
        still refused.
        """
        for rel in deleted:
            entry = self._contained_entry(rel)
            if entry is not None and (entry.is_file() or entry.is_symlink()):
                entry.unlink()

    def _contained_target(self, rel: str) -> Path | None:
        """The real-tree path for a WRITE of ``rel``, or ``None`` if it resolves outside ``working_dir``.

        Fully resolves ``working_dir / rel`` (following any symlink) and refuses it unless it stays within the
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

    def _contained_entry(self, rel: str) -> Path | None:
        """The real-tree entry for a DELETE of ``rel``, or ``None`` if its PARENT dir escapes ``working_dir``.

        For a delete we resolve only the parent directory (not the final component), so a symlink entry is
        removed as the link itself rather than followed; a path whose parent traverses a symlink out of the
        working_dir is still refused.
        """
        base = self._working_dir.resolve()
        candidate = self._working_dir / rel
        try:
            parent = candidate.parent.resolve()
        except OSError:
            return None
        if parent == base or parent.is_relative_to(base):
            return candidate
        _log.warning("sandbox delete-back skipped %s: its parent resolves outside the working_dir", rel)
        return None

    # --- non-git temp-copy strategy -----------------------------------------

    def _copy_changes(self) -> tuple[str, list[str], list[str]]:
        """Diff the temp copy against the OPEN-TIME baseline: created/edited files, deleted files, and a text diff.

        Returns ``(diff, changed, deleted)``. ``changed`` is every file in the copy whose bytes differ from the
        baseline snapshot taken at open (or that is new vs it) -- i.e. what the AGENT changed, NOT what merely
        differs from the live tree, so a concurrent user edit to an untouched file is never mis-attributed.
        ``deleted`` is every baseline path now gone from the copy. The diff is best-effort text (binary files
        are listed but rendered as a one-line marker), since this path has no git to produce a binary patch; the
        authoritative output is the changed/deleted lists the apply-back uses.
        """
        changed: list[str] = []
        diff_parts: list[str] = []
        in_copy: set[str] = set()
        for path in _walk_files(self._root):
            rel = path.relative_to(self._root).as_posix()
            in_copy.add(rel)
            new_bytes = path.read_bytes()
            if self._baseline.get(rel) == _sha256(new_bytes):
                continue  # unchanged vs the open-time baseline -- the agent did not touch it
            changed.append(rel)
            diff_parts.append(_text_file_diff(rel, self._working_dir / rel, new_bytes))
        deleted: list[str] = []
        for rel in self._baseline:
            if rel not in in_copy:
                deleted.append(rel)
                diff_parts.append(f"--- a/{rel}\n+++ /dev/null\n(file deleted)")
        return "\n".join(diff_parts), sorted(changed), sorted(deleted)

    def _copy_apply(self, changed: list[str]) -> None:
        """Copy each changed/created file from the sandbox back into the real ``working_dir`` (contained).

        Never FOLLOWS a destination symlink: if the destination is itself a symlink, the link is removed and the
        agent's file is written at the link's own (in-tree) location, so a symlink cannot redirect the write to
        another file (in or out of the tree) that the conflict checks never examined. A destination that is a
        real directory is skipped rather than copied into.
        """
        for rel in changed:
            dst = self._contained_target(rel)
            if dst is None:
                continue  # resolves outside the working_dir via a symlink -- refused
            if dst.is_symlink():
                dst.unlink()  # replace the link with the agent's file; do not write THROUGH it
            elif dst.is_dir():
                _log.warning("sandbox apply-back skipped %s: a real directory exists at the destination", rel)
                continue
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
        # Snapshot the copied tree's content hashes as the open-time baseline, so the non-git change detection
        # is agent-relative and a concurrent edit during the turn is caught at apply (the copy == the real tree
        # right now, so this is the real tree's state at open time).
        baseline = {path.relative_to(root).as_posix(): _sha256(path.read_bytes()) for path in _walk_files(root)}
        return Sandbox(manager=self, working_dir=resolved, root=root, is_git=False, baseline=baseline)

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
        """Run a git subcommand with autocrlf normalization OFF (byte-faithful sandbox diff / apply)."""
        return self._git(cwd, *args)

    def run_git_user_config(self, cwd: Path, *args: str) -> str:
        """Run a git subcommand under the repo's OWN config (autocrlf as the user set it).

        Used only for the apply-back clobber check: whether the user's real working tree has an uncommitted
        edit must reflect what the user's git considers dirty. Forcing ``core.autocrlf=false`` here would
        falsely flag a file that git merely checked out with CRLF under ``autocrlf=true`` as modified, so the
        check uses the repo's native line-ending policy.
        """
        return self._git(cwd, *args, force_no_autocrlf=False)

    def _git(self, cwd: Path, *args: str, force_no_autocrlf: bool = True) -> str:
        """Run ``git -C <cwd> <args>`` and return stdout; raise :class:`RutherfordError` on a non-zero exit.

        The single git entry point -- one ``subprocess.run`` with a hard timeout, text I/O, and no shell. A
        non-zero exit or a launch failure (git missing) becomes a typed error the caller maps to an envelope,
        never a silent partial sandbox. ``core.autocrlf=false`` is forced by default so the staged diff stays
        byte-faithful on Windows (the worktree blob is not line-ending-normalized), matching the byte-for-byte
        file copy used to apply the changes back; ``force_no_autocrlf=False`` runs under the repo's own policy
        (the clobber check, which must agree with the user's ``git status``).
        """
        prefix = ["-c", "core.autocrlf=false"] if force_no_autocrlf else []
        cmd = ["git", *prefix, "-C", str(cwd), *args]
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
        """Copy ``working_dir`` (minus the excluded dirs and any symlinks) into a temp dir, size-guarded first.

        Refuses a tree whose copyable content exceeds :data:`_MAX_COPY_BYTES` with a clear error -- write mode
        on a huge non-git dir should use a git working_dir, not a slow, unreliable copy. The excluded dirs
        (``.git`` is absent here by definition, plus ``node_modules`` / virtualenvs / caches) are skipped so
        the copy is the source, not its regenerable artifacts. SYMLINKS are skipped entirely: copying them as
        links would need an OS privilege (Windows), and dereferencing them (copytree's default) would pull the
        bytes of a file OUTSIDE the working_dir into the sandbox -- so they are simply not copied, and the real
        symlinks are left untouched by the apply-back.
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
        shutil.copytree(working_dir, copy, ignore=_copy_ignore, dirs_exist_ok=True)
        return copy


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` ignore callback: skip the excluded dirs AND any symlink entry.

    Returning the symlink names here means a symlink in the working_dir is neither dereferenced (which would
    copy outside bytes into the sandbox) nor recreated (which needs an OS privilege on Windows) -- it is simply
    absent from the copy, and the real symlink is left untouched.
    """
    skip = {name for name in names if name in _COPY_EXCLUDES}
    base = Path(directory)
    skip.update(name for name in names if (base / name).is_symlink())
    return skip


def _sha256(data: bytes) -> str:
    """The hex sha256 of ``data`` -- the non-git baseline's per-file content fingerprint."""
    return hashlib.sha256(data).hexdigest()


def _walk_files(root: Path) -> list[Path]:
    """Every regular file under ``root``, skipping the excluded dirs and symlinks, sorted (deterministic diff).

    Symlinks are skipped (``is_symlink``) so a link is never followed into content outside the tree and never
    mistaken for a regular file the agent edited.
    """
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
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
