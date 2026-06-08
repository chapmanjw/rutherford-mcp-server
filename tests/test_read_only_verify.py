# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the opt-in read_only verification (post-run git-tree check)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.models import DelegationRequest, InvocationSpec, ProcessResult, Target
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter


class _WritingRunner:
    """A runner that optionally writes a file into the working dir, to simulate a mutating run."""

    def __init__(self, *, write: bool) -> None:
        self._write = write

    async def run(
        self, spec: InvocationSpec, timeout_s: float, on_progress: Callable[[str], None] | None = None
    ) -> ProcessResult:
        if self._write and spec.cwd:
            (Path(spec.cwd) / "scratch.txt").write_text("mutated by a read_only run", encoding="utf-8")
        return ProcessResult(exit_code=0, stdout="done")


@pytest.fixture
def git_repo(tmp_path: Path) -> Iterator[Path]:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    yield tmp_path


def _service(runner: _WritingRunner, cfg: RutherfordConfig) -> DelegationService:
    return DelegationService(AdapterRegistry([FakeAdapter("a")]), runner, cfg, load_roles())


async def test_read_only_violation_is_flagged_when_enabled(git_repo: Path) -> None:
    service = _service(_WritingRunner(write=True), RutherfordConfig(verify_read_only=True))
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q", working_dir=str(git_repo)))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "READONLY_VIOLATED"


async def test_read_only_clean_run_passes(git_repo: Path) -> None:
    service = _service(_WritingRunner(write=False), RutherfordConfig(verify_read_only=True))
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q", working_dir=str(git_repo)))
    assert result.ok
    assert result.text == "done"


async def test_read_only_verification_is_opt_in(git_repo: Path) -> None:
    # Off by default: even a mutation is not flagged when verify_read_only is False.
    service = _service(_WritingRunner(write=True), RutherfordConfig(verify_read_only=False))
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q", working_dir=str(git_repo)))
    assert result.ok


async def test_read_only_skipped_for_non_git_dir(tmp_path: Path) -> None:
    # A non-git working dir cannot be snapshotted cheaply, so verification is skipped (not flagged).
    service = _service(_WritingRunner(write=True), RutherfordConfig(verify_read_only=True))
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q", working_dir=str(tmp_path)))
    assert result.ok
