# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the opt-in read_only verification (post-run git-tree fingerprint)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import DelegationRequest, DelegationResult, InvocationSpec, ProcessResult, Target
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter


class _Runner:
    """A runner that runs an optional side effect against the working dir, then returns a result.

    The side effect simulates what the delegated CLI did to the tree (write a file, edit one, write
    outside the working dir). ``exit_code`` lets a test simulate a run that failed *and* mutated.
    """

    def __init__(self, *, side_effect: Callable[[Path], object] | None = None, exit_code: int = 0) -> None:
        self._side_effect = side_effect
        self._exit_code = exit_code

    async def run(
        self, spec: InvocationSpec, timeout_s: float, on_progress: Callable[[str], None] | None = None
    ) -> ProcessResult:
        if self._side_effect is not None and spec.cwd:
            self._side_effect(Path(spec.cwd))
        return ProcessResult(exit_code=self._exit_code, stdout="done" if self._exit_code == 0 else "")


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    yield tmp_path


def _commit(repo: Path, rel: str, content: str) -> None:
    """Write, stage, and commit ``rel`` so a later edit can be made to an already-tracked file."""
    (repo / rel).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", rel], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t.test", "-c", "user.name=Test", "commit", "-m", "c"],
        capture_output=True,
        check=True,
    )


def _service(runner: _Runner, cfg: RutherfordConfig) -> DelegationService:
    return DelegationService(AdapterRegistry([FakeAdapter("a")]), runner, cfg, load_roles())


async def _delegate(
    runner: _Runner, working_dir: Path, *, safety_mode: SafetyMode = SafetyMode.READ_ONLY, verify: bool = True
) -> DelegationResult:
    service = _service(runner, RutherfordConfig(verify_read_only=verify))
    return await service.delegate(
        DelegationRequest(target=Target(cli="a"), prompt="q", working_dir=str(working_dir), safety_mode=safety_mode)
    )


# --- the basic gate ----------------------------------------------------------


async def test_read_only_violation_is_flagged_when_enabled(git_repo: Path) -> None:
    runner = _Runner(side_effect=lambda cwd: (cwd / "scratch.txt").write_text("written", encoding="utf-8"))
    result = await _delegate(runner, git_repo)
    assert not result.ok
    assert result.error is not None and result.error.code == "READONLY_VIOLATED"


async def test_read_only_clean_run_passes(git_repo: Path) -> None:
    result = await _delegate(_Runner(), git_repo)
    assert result.ok
    assert result.text == "done"


async def test_read_only_verification_is_opt_in(git_repo: Path) -> None:
    runner = _Runner(side_effect=lambda cwd: (cwd / "scratch.txt").write_text("written", encoding="utf-8"))
    result = await _delegate(runner, git_repo, verify=False)
    assert result.ok  # off by default: even a mutation is not flagged


async def test_read_only_skipped_for_non_git_dir(tmp_path: Path) -> None:
    runner = _Runner(side_effect=lambda cwd: (cwd / "scratch.txt").write_text("written", encoding="utf-8"))
    result = await _delegate(runner, tmp_path)
    assert result.ok  # a non-git dir cannot be fingerprinted cheaply, so verification is skipped


# --- the hardening the bulletproofing audit drove ----------------------------


async def test_further_edit_to_an_already_dirty_file_is_flagged(git_repo: Path) -> None:
    # Status codes alone miss this: the file is already " M" before the run and stays " M" after, but
    # the content fingerprint (diff) changes, so the further edit is caught.
    _commit(git_repo, "file.txt", "line1\n")
    (git_repo / "file.txt").write_text("line1\nline2\n", encoding="utf-8")  # pre-existing dirt
    runner = _Runner(side_effect=lambda cwd: (cwd / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8"))
    result = await _delegate(runner, git_repo)
    assert not result.ok and result.error is not None and result.error.code == "READONLY_VIOLATED"


async def test_pre_existing_dirt_is_not_re_attributed(git_repo: Path) -> None:
    # A tree that was already dirty before the run, where the run changes nothing, must pass.
    _commit(git_repo, "file.txt", "line1\n")
    (git_repo / "file.txt").write_text("line1\nline2\n", encoding="utf-8")
    result = await _delegate(_Runner(), git_repo)
    assert result.ok


async def test_write_to_a_gitignored_path_is_flagged(git_repo: Path) -> None:
    # The sensitive blind spot: a read_only run writing a gitignored side file (.env, a cache dir).
    (git_repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")

    def write_ignored(cwd: Path) -> None:
        (cwd / "ignored").mkdir(exist_ok=True)
        (cwd / "ignored" / "secret.txt").write_text("leak", encoding="utf-8")

    result = await _delegate(_Runner(side_effect=write_ignored), git_repo)
    assert not result.ok and result.error is not None and result.error.code == "READONLY_VIOLATED"


async def test_change_outside_the_working_dir_subtree_is_not_flagged(git_repo: Path) -> None:
    # working_dir is a subdirectory; an unrelated change elsewhere in the repo must not be attributed
    # to this delegation (the fingerprint is scoped to the working_dir subtree).
    (git_repo / "sub").mkdir()
    runner = _Runner(side_effect=lambda cwd: (cwd.parent / "README.md").write_text("touched", encoding="utf-8"))
    result = await _delegate(runner, git_repo / "sub")
    assert result.ok


async def test_propose_mode_is_also_verified(git_repo: Path) -> None:
    # propose is non-mutating, so the verify gate applies to it too.
    runner = _Runner(side_effect=lambda cwd: (cwd / "scratch.txt").write_text("written", encoding="utf-8"))
    result = await _delegate(runner, git_repo, safety_mode=SafetyMode.PROPOSE)
    assert not result.ok and result.error is not None and result.error.code == "READONLY_VIOLATED"


async def test_a_failed_run_keeps_its_real_error_even_if_it_touched_the_tree(git_repo: Path) -> None:
    # A run that already failed (non-zero exit) keeps its real error; the read_only check must not
    # overwrite it with READONLY_VIOLATED and hide why the run failed.
    runner = _Runner(side_effect=lambda cwd: (cwd / "scratch.txt").write_text("partial", encoding="utf-8"), exit_code=2)
    result = await _delegate(runner, git_repo)
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"
