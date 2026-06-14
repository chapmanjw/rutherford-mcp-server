# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit tests for the write/propose sandbox substrate: worktree isolation, the FileGateway path guard,
the TerminalBroker, and the ``verify_read_only`` post-run check.

These drive the REAL fake ACP agent subprocess (``tests.fake_acp_agent``) with its ``WRITE=`` / ``RUN=``
triggers, so the whole sandbox path -- worktree create, the agent's ``fs/write`` landing in the worktree, the
diff, the apply-back, the path-escape rejection, the terminal broker -- is exercised without a real model.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.sandbox import SandboxManager
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationRequest, Target
from rutherford.services.delegation import DelegationService

_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
_FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)


def _service(config: RutherfordConfig | None = None) -> DelegationService:
    return DelegationService(DescriptorRegistry([_FAKE]), config or RutherfordConfig())


def _git(path: Path, *args: str) -> str:
    """Run a git command in ``path`` (a sync helper, so async tests do not trip the blocking-call lint)."""
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout


def _git_repo(path: Path) -> None:
    """Initialise a git repo at ``path`` with one commit, so a detached worktree can be added off HEAD."""
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")


async def _delegate(
    service: DelegationService, *, prompt: str, working_dir: Path, mode: SafetyMode
) -> DelegationRequest:
    return DelegationRequest(
        target=Target(cli="fake"),
        prompt=prompt,
        working_dir=str(working_dir),
        safety_mode=mode,
        trust_workspace=True,
        timeout_s=30.0,
    )


# --- write mode: edit lands in the real working_dir --------------------------


async def test_write_mode_applies_a_new_file_back_to_the_working_dir(tmp_path: Path) -> None:
    """A write-mode delegation whose agent writes a file ends with that file in the real working_dir."""
    _git_repo(tmp_path)
    service = _service()
    req = await _delegate(service, prompt="WRITE=hello.txt:hello world", working_dir=tmp_path, mode=SafetyMode.WRITE)
    result = await service.delegate(req)
    assert result.ok is True, f"write delegation failed: {result.error}"
    landed = tmp_path / "hello.txt"
    assert landed.is_file(), "the agent's file did not land back in the real working_dir"
    assert landed.read_text(encoding="utf-8") == "hello world"
    assert result.changed_files == ["hello.txt"]
    assert result.changes_applied is True
    assert result.diff and "hello.txt" in result.diff


async def test_write_mode_edits_an_existing_tracked_file(tmp_path: Path) -> None:
    """A write-mode edit to a tracked file is applied back, and the diff/changed_files reflect it."""
    _git_repo(tmp_path)
    service = _service()
    req = await _delegate(service, prompt="WRITE=README.md:changed line", working_dir=tmp_path, mode=SafetyMode.WRITE)
    result = await service.delegate(req)
    assert result.ok is True, f"write delegation failed: {result.error}"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "changed line"
    assert result.changed_files == ["README.md"]
    assert result.changes_applied is True


# --- propose mode: real tree untouched, diff returned ------------------------


async def test_propose_mode_returns_a_diff_and_leaves_the_real_tree_unchanged(tmp_path: Path) -> None:
    """A propose-mode delegation returns the patch + changed_files but never touches the real working_dir."""
    _git_repo(tmp_path)
    service = _service()
    req = await _delegate(
        service, prompt="WRITE=proposed.txt:a proposal", working_dir=tmp_path, mode=SafetyMode.PROPOSE
    )
    result = await service.delegate(req)
    assert result.ok is True, f"propose delegation failed: {result.error}"
    # The real working_dir must be untouched: the proposed file does not exist on disk.
    assert not (tmp_path / "proposed.txt").exists(), "propose mode wrote to the real working_dir"
    assert result.changed_files == ["proposed.txt"]
    assert result.changes_applied is False  # nothing applied
    assert result.diff and "proposed.txt" in result.diff
    # The git tree is still clean (only the seed commit, no working changes).
    assert _git(tmp_path, "status", "--porcelain").strip() == "", "propose left the tree dirty"


# --- the FileGateway path-escape guard ---------------------------------------


@pytest.mark.parametrize("escape", ["../evil.txt", "../../evil.txt"])
async def test_write_escaping_the_sandbox_root_is_rejected(tmp_path: Path, escape: str) -> None:
    """A write to a path that climbs out of the sandbox root is rejected; nothing lands outside the tree."""
    _git_repo(tmp_path)
    service = _service()
    req = await _delegate(service, prompt=f"WRITE={escape}:pwned", working_dir=tmp_path, mode=SafetyMode.WRITE)
    result = await service.delegate(req)
    # The turn itself succeeds (the agent reports the denial in its answer); the escape never lands.
    assert "denied" in result.text.lower() or "escape" in result.text.lower(), (
        f"expected a denial in the agent answer, got: {result.text!r}"
    )
    assert not (tmp_path.parent / "evil.txt").exists()
    assert (result.changed_files or []) == []  # the rejected write produced no change


async def test_write_to_an_absolute_path_outside_the_root_is_rejected(tmp_path: Path) -> None:
    """An absolute path outside the sandbox root is rejected by the FileGateway (client-level check).

    Driven at the client directly rather than through the ``WRITE=`` trigger, because a Windows absolute path
    carries a drive colon (``C:``) that the trigger's first-``:`` split would mangle. The point is the same:
    a write whose ABSOLUTE target is outside the sandbox root is refused and journaled.
    """
    from acp import RequestError

    from rutherford.acp.client import RutherfordACPClient
    from rutherford.acp.journal import EventJournal

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    journal = EventJournal()
    client = RutherfordACPClient(
        journal=journal,
        policy=PermissionPolicy(SafetyMode.WRITE, sandboxed=True),
        cwd=str(root),
        sandbox_root=str(root),
    )
    with pytest.raises(RequestError):
        await client.write_text_file("pwned", str(outside), "s")
    assert not outside.exists()
    assert "fs_write_denied" in journal.kinds()


# --- the TerminalBroker: write allows, read_only/propose deny ----------------


async def test_terminal_runs_in_write_mode(tmp_path: Path) -> None:
    """A write-mode terminal command runs in the sandbox and reports its exit code."""
    _git_repo(tmp_path)
    service = _service()
    # A trivial, portable command: the same interpreter exits 0.
    req = await _delegate(service, prompt=f"RUN={sys.executable} -c pass", working_dir=tmp_path, mode=SafetyMode.WRITE)
    result = await service.delegate(req)
    assert result.ok is True, f"terminal delegation failed: {result.error}"
    assert "exit 0" in result.text, f"expected a clean exit, got: {result.text!r}"


async def test_terminal_is_denied_in_read_only_and_propose(tmp_path: Path) -> None:
    """A terminal command is denied in read_only and in propose (terminals are write-capable)."""
    _git_repo(tmp_path)
    service = _service()
    for mode in (SafetyMode.READ_ONLY, SafetyMode.PROPOSE):
        req = await _delegate(service, prompt=f"RUN={sys.executable} -c pass", working_dir=tmp_path, mode=mode)
        result = await service.delegate(req)
        assert "terminal denied" in result.text.lower(), f"{mode}: expected a terminal denial, got {result.text!r}"


# --- non-git working_dir: temp-copy fallback ---------------------------------


async def test_write_mode_non_git_dir_copies_changes_back(tmp_path: Path) -> None:
    """A write-mode delegation in a NON-git dir runs in a temp copy and copies the changed file back."""
    # tmp_path is not a git repo (no _git_repo call), so the temp-copy strategy is exercised.
    (tmp_path / "existing.txt").write_text("original\n", encoding="utf-8")
    service = _service()
    req = await _delegate(
        service, prompt="WRITE=created.txt:from the copy", working_dir=tmp_path, mode=SafetyMode.WRITE
    )
    result = await service.delegate(req)
    assert result.ok is True, f"non-git write failed: {result.error}"
    landed = tmp_path / "created.txt"
    assert landed.is_file(), "the non-git temp-copy change did not come back"
    assert landed.read_text(encoding="utf-8") == "from the copy"
    assert result.changed_files == ["created.txt"]


async def test_propose_mode_non_git_dir_leaves_the_real_dir_untouched(tmp_path: Path) -> None:
    """A propose-mode delegation in a non-git dir reports the change but does not touch the real dir."""
    service = _service()
    req = await _delegate(service, prompt="WRITE=proposed.txt:proposal", working_dir=tmp_path, mode=SafetyMode.PROPOSE)
    result = await service.delegate(req)
    assert result.ok is True, f"non-git propose failed: {result.error}"
    assert not (tmp_path / "proposed.txt").exists(), "non-git propose wrote to the real dir"
    assert result.changed_files == ["proposed.txt"]
    assert result.changes_applied is False


# --- verify_read_only --------------------------------------------------------


async def test_verify_read_only_passes_a_clean_run(tmp_path: Path) -> None:
    """A clean read_only delegation against an unchanged git tree is not flagged when verify is on."""
    _git_repo(tmp_path)
    service = _service(RutherfordConfig(verify_read_only=True))
    req = DelegationRequest(
        target=Target(cli="fake"),
        prompt="17 + 25",
        working_dir=str(tmp_path),
        safety_mode=SafetyMode.READ_ONLY,
        timeout_s=30.0,
    )
    result = await service.delegate(req)
    assert result.ok is True, f"clean read-only run was wrongly flagged: {result.error}"


async def test_verify_read_only_flags_a_tree_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read_only delegation whose tree changed across the turn fails with READONLY_VIOLATED.

    read_only denies fs/write at the client AND the terminal, so the agent cannot mutate the tree through the
    sanctioned channels -- the threat verify_read_only exists for is the agent touching the disk OUT OF BAND
    (its own OS process, not the ACP callbacks). That is simulated by making the before/after fingerprint
    differ: the after-snapshot returns a changed value, as if the agent wrote a file during the turn.
    """
    _git_repo(tmp_path)
    service = _service(RutherfordConfig(verify_read_only=True))
    from rutherford.services import delegation as delegation_mod

    calls = {"n": 0}
    real = delegation_mod._git_fingerprint

    def _fingerprint(working_dir: str) -> str | None:
        calls["n"] += 1
        base = real(working_dir)
        # First call (before): the real snapshot. Second call (after): a changed snapshot.
        return base if calls["n"] == 1 else (base or "") + "\nMUTATED"

    monkeypatch.setattr(delegation_mod, "_git_fingerprint", _fingerprint)
    req = DelegationRequest(
        target=Target(cli="fake"),
        prompt="17 + 25",
        working_dir=str(tmp_path),
        safety_mode=SafetyMode.READ_ONLY,
        timeout_s=30.0,
    )
    result = await service.delegate(req)
    assert result.ok is False, "a tree change under a read-only run was not flagged"
    assert result.error is not None and result.error.code is ErrorCode.READONLY_VIOLATED


def test_verify_read_only_fingerprint_detects_a_change(tmp_path: Path) -> None:
    """The git fingerprint differs before vs after an out-of-band write (the READONLY_VIOLATED mechanism)."""
    from rutherford.services.delegation import _git_fingerprint

    _git_repo(tmp_path)
    before = _git_fingerprint(str(tmp_path))
    assert before is not None
    (tmp_path / "snuck-in.txt").write_text("side effect\n", encoding="utf-8")
    after = _git_fingerprint(str(tmp_path))
    assert after is not None
    assert after != before, "the fingerprint did not detect a new file"


def test_verify_read_only_fingerprint_none_for_non_git(tmp_path: Path) -> None:
    """The fingerprint is None for a non-git dir, so verify_read_only degrades to a no-op there."""
    from rutherford.services.delegation import _git_fingerprint

    assert _git_fingerprint(str(tmp_path)) is None


# --- worktree lifecycle / cleanup --------------------------------------------


def test_sandbox_worktree_is_created_and_cleaned_up(tmp_path: Path) -> None:
    """SandboxManager.open creates a worktree off HEAD; cleanup removes it and prunes the repo's record."""
    _git_repo(tmp_path)
    manager = SandboxManager()
    sandbox = manager.open(str(tmp_path))
    root = Path(sandbox.root)
    assert root.is_dir(), "the worktree root was not created"
    assert (root / "README.md").read_text(encoding="utf-8") == "seed\n", "the worktree did not start from HEAD"
    # git knows about the worktree while it is live.
    listed = subprocess.run(["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert str(root) in listed.stdout.replace("/", "\\") or str(root) in listed.stdout
    sandbox.cleanup()
    assert not root.exists(), "the worktree dir survived cleanup"
    # The repo no longer lists the removed worktree.
    after = subprocess.run(["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert str(root) not in after.stdout, "git still lists the removed worktree"


def test_sandbox_finish_with_no_change_is_empty(tmp_path: Path) -> None:
    """A sandbox whose agent changed nothing yields an empty result (no diff, no changed files, not applied)."""
    _git_repo(tmp_path)
    manager = SandboxManager()
    sandbox = manager.open(str(tmp_path))
    try:
        outcome = sandbox.finish(SafetyMode.WRITE)
    finally:
        sandbox.cleanup()
    assert outcome.changed_files == []
    assert outcome.diff == ""
    assert outcome.applied is False


def test_sandbox_non_git_copy_guard_refuses_a_huge_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-git tree over the copy-size guard is refused with a clear error rather than copied."""
    from rutherford.acp import sandbox as sandbox_mod
    from rutherford.domain.errors import RutherfordError

    (tmp_path / "big.bin").write_bytes(b"x" * 1024)
    monkeypatch.setattr(sandbox_mod, "_MAX_COPY_BYTES", 100)  # force the guard to trip
    manager = SandboxManager()
    with pytest.raises(RutherfordError) as exc:
        manager.open(str(tmp_path))
    assert exc.value.code is ErrorCode.WORKSPACE_NOT_TRUSTED
    assert "git working_dir" in exc.value.message


async def test_non_git_copy_guard_surfaces_as_a_failed_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the copy guard trips, the delegation fails cleanly rather than running unsandboxed."""
    from rutherford.acp import sandbox as sandbox_mod

    (tmp_path / "big.bin").write_bytes(b"x" * 1024)
    monkeypatch.setattr(sandbox_mod, "_MAX_COPY_BYTES", 100)
    service = _service()
    req = await _delegate(service, prompt="WRITE=created.txt:nope", working_dir=tmp_path, mode=SafetyMode.WRITE)
    result = await service.delegate(req)
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.WORKSPACE_NOT_TRUSTED
    assert not (tmp_path / "created.txt").exists()


# --- direct client / permission unit checks ----------------------------------


def test_permission_propose_allows_writes_only_when_sandboxed() -> None:
    """propose denies writes un-sandboxed (read-only-equivalent) and allows them inside a sandbox."""
    assert PermissionPolicy(SafetyMode.PROPOSE, sandboxed=False).allow_writes is False
    assert PermissionPolicy(SafetyMode.PROPOSE, sandboxed=True).allow_writes is True
    assert PermissionPolicy(SafetyMode.READ_ONLY, sandboxed=True).allow_writes is False
    assert PermissionPolicy(SafetyMode.WRITE, sandboxed=False).allow_writes is True


async def test_terminal_broker_captures_output_and_exit(tmp_path: Path) -> None:
    """The TerminalBroker runs a command in the root, captures its output, and reports the exit code."""
    from rutherford.acp.client import TerminalBroker

    broker = TerminalBroker(tmp_path)
    term_id = await broker.create(sys.executable, ["-c", "print('hi from cmd')"], None)
    exit_resp = await broker.wait_exit(term_id)
    out = broker.output(term_id)
    await broker.release(term_id)
    await broker.shutdown()
    assert exit_resp.exit_code == 0
    assert "hi from cmd" in out.output
    assert out.exit_status is not None and out.exit_status.exit_code == 0


async def test_terminal_broker_unknown_id_and_failed_spawn(tmp_path: Path) -> None:
    """An unknown terminal id and an unspawnnable command both surface a clean RequestError."""
    from acp import RequestError

    from rutherford.acp.client import TerminalBroker

    broker = TerminalBroker(tmp_path)
    with pytest.raises(RequestError):
        broker.output("no-such-term")
    with pytest.raises(RequestError):
        await broker.create("this-binary-does-not-exist-xyz123", None, None)


def test_write_mode_deletes_a_file_back(tmp_path: Path) -> None:
    """A write-mode agent that removes a tracked file in the sandbox has the deletion applied back.

    Exercised by deleting in the sandbox via the SandboxManager directly (the fake agent only writes), proving
    the apply-back honours deletions, not just adds.
    """
    _git_repo(tmp_path)
    (tmp_path / "doomed.txt").write_text("bye\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add doomed")
    manager = SandboxManager()
    sandbox = manager.open(str(tmp_path))
    try:
        (Path(sandbox.root) / "doomed.txt").unlink()
        outcome = sandbox.finish(SafetyMode.WRITE)
    finally:
        sandbox.cleanup()
    assert "doomed.txt" in outcome.changed_files
    assert outcome.applied is True
    assert not (tmp_path / "doomed.txt").exists(), "the deletion was not applied back to the real tree"


def test_non_git_copy_diff_lists_created_and_edited(tmp_path: Path) -> None:
    """The non-git copy path produces a changed-file list and a text diff for created and edited files."""
    (tmp_path / "keep.txt").write_text("unchanged\n", encoding="utf-8")
    (tmp_path / "edit.txt").write_text("before\n", encoding="utf-8")
    manager = SandboxManager()
    sandbox = manager.open(str(tmp_path))  # non-git -> temp copy
    root = Path(sandbox.root)
    (root / "edit.txt").write_text("after\n", encoding="utf-8")
    (root / "new.txt").write_text("created\n", encoding="utf-8")
    try:
        outcome = sandbox.finish(SafetyMode.PROPOSE)  # propose: diff only, nothing applied
    finally:
        sandbox.cleanup()
    assert outcome.changed_files == ["edit.txt", "new.txt"]
    assert outcome.applied is False
    assert "edit.txt" in outcome.diff and "new.txt" in outcome.diff
    # propose did not touch the real dir.
    assert (tmp_path / "edit.txt").read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / "new.txt").exists()


def test_git_repo_with_no_commit_falls_back_to_copy(tmp_path: Path) -> None:
    """A git repo with no commit yet (no HEAD) cannot host a worktree, so the copy strategy is used."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "wip.txt").write_text("work in progress\n", encoding="utf-8")
    manager = SandboxManager()
    sandbox = manager.open(str(tmp_path))
    root = Path(sandbox.root)
    try:
        # The copy strategy was used: the root is a plain temp copy, not a git worktree.
        assert not (root / ".git").exists()
        assert (root / "wip.txt").read_text(encoding="utf-8") == "work in progress\n"
    finally:
        sandbox.cleanup()
    assert not root.exists()


async def test_sandboxed_read_confined_to_root_rejects_escape(tmp_path: Path) -> None:
    """A sandboxed client serves a read inside the root but rejects one that escapes it."""
    from acp import RequestError

    from rutherford.acp.client import RutherfordACPClient
    from rutherford.acp.journal import EventJournal

    root = tmp_path / "root"
    root.mkdir()
    (root / "inside.txt").write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    journal = EventJournal()
    client = RutherfordACPClient(
        journal=journal,
        policy=PermissionPolicy(SafetyMode.WRITE, sandboxed=True),
        cwd=str(root),
        sandbox_root=str(root),
    )
    served = await client.read_text_file(str(root / "inside.txt"), "s")
    assert served.content == "ok\n"
    with pytest.raises(RequestError):
        await client.read_text_file(str(outside), "s")
    assert "fs_read_denied" in journal.kinds()


# --- hardening (found by the Codex-via-Rutherford safety review) -------------


@pytest.mark.parametrize("mode", [SafetyMode.PROPOSE, SafetyMode.WRITE, SafetyMode.YOLO])
async def test_sandboxed_mode_without_a_working_dir_is_refused(tmp_path: Path, mode: SafetyMode) -> None:
    """A sandboxed mode (propose/write/yolo) with NO working_dir is refused -- never run unsandboxed in cwd.

    Without a working_dir there is no tree to build the sandbox from, so the turn would fall through to the
    direct path in the server's own cwd with writes allowed. ``trust_workspace=True`` is set to prove the guard
    is independent of the trust gate (the gate alone passes trust_workspace through, working_dir or not).
    """
    service = _service()
    req = DelegationRequest(
        target=Target(cli="fake"),
        prompt="WRITE=escape.txt:nope",
        safety_mode=mode,
        trust_workspace=True,
        timeout_s=30.0,
    )
    result = await service.delegate(req)
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT
    assert "working_dir" in result.error.message


def test_apply_back_refuses_a_relpath_that_climbs_out_of_the_working_dir(tmp_path: Path) -> None:
    """The apply-back containment guard refuses a relpath that resolves outside the working_dir (`..` climb)."""
    from rutherford.acp.sandbox import Sandbox

    work = tmp_path / "work"
    work.mkdir()
    sandbox = Sandbox(manager=SandboxManager(), working_dir=work.resolve(), root=work, is_git=False)
    assert sandbox._contained_target("../evil.txt") is None  # climbs out -> refused
    inside = sandbox._contained_target("sub/ok.txt")
    assert inside is not None and inside.name == "ok.txt"  # a normal nested path is allowed


def test_apply_back_refuses_writing_through_a_symlink_escape(tmp_path: Path) -> None:
    """The containment guard refuses an apply-back path that resolves outside the tree via a symlink.

    Without the guard, a symlink inside the workspace (``link -> /outside``) would let an edit to ``link/x``
    follow the symlink and overwrite a file OUTSIDE the tree the user trusted -- a write escape.
    """
    from rutherford.acp.sandbox import Sandbox

    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    try:
        (work / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not creatable on this platform / without the privilege")
    sandbox = Sandbox(manager=SandboxManager(), working_dir=work.resolve(), root=work, is_git=False)
    assert sandbox._contained_target("link/evil.txt") is None  # follows the symlink outside -> refused
    assert not (outside / "evil.txt").exists()


def test_non_git_copy_detects_and_applies_a_deletion(tmp_path: Path) -> None:
    """The non-git copy path now detects a file deleted in the sandbox and applies the deletion back.

    Previously the non-git temp-copy diff only saw created/edited files, so a write/yolo agent that DELETED a
    file in the sandbox had the deletion silently lost. It now mirrors the git path: the original file gone
    from the copy is reported as deleted and removed from the real tree on apply.
    """
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "doomed.txt").write_text("bye\n", encoding="utf-8")
    manager = SandboxManager()
    sandbox = manager.open(str(tmp_path))  # non-git -> temp copy
    (Path(sandbox.root) / "doomed.txt").unlink()  # the agent removes it in the sandbox
    try:
        outcome = sandbox.finish(SafetyMode.WRITE)
    finally:
        sandbox.cleanup()
    assert "doomed.txt" in outcome.changed_files
    assert outcome.applied is True
    assert not (tmp_path / "doomed.txt").exists(), "the non-git deletion was not applied back"
    assert (tmp_path / "keep.txt").exists(), "an unrelated file must survive the deletion apply"


async def test_verify_read_only_flags_a_change_even_on_a_failed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_read_only fingerprints the tree whether or not the turn SUCCEEDED.

    A read-only turn that mutated the tree and then failed (or returned empty) still broke the read-only
    promise; the side effect is the signal that matters, so it must surface as READONLY_VIOLATED rather than an
    ordinary failure that hides the write. The DEAD agent fails at handshake; a fingerprint that differs across
    the turn stands in for the out-of-band mutation.
    """
    _git_repo(tmp_path)
    dead = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
    service = DelegationService(DescriptorRegistry([dead]), RutherfordConfig(verify_read_only=True))
    from rutherford.services import delegation as delegation_mod

    calls = {"n": 0}
    real = delegation_mod._git_fingerprint

    def _fingerprint(working_dir: str) -> str | None:
        calls["n"] += 1
        base = real(working_dir)
        return base if calls["n"] == 1 else (base or "") + "\nMUTATED"  # after-snapshot differs

    monkeypatch.setattr(delegation_mod, "_git_fingerprint", _fingerprint)
    req = DelegationRequest(
        target=Target(cli="dead"),
        prompt="17 + 25",
        working_dir=str(tmp_path),
        safety_mode=SafetyMode.READ_ONLY,
        timeout_s=30.0,
    )
    result = await service.delegate(req)
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.READONLY_VIOLATED
