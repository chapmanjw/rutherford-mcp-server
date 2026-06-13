# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for durable run persistence in the delegation service (F2), driven by fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import JobStatus, SafetyMode
from rutherford.domain.models import (
    ConsensusRequest,
    DelegationRequest,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    RunRecord,
    Target,
)
from rutherford.io.ledger import RunLedger
from rutherford.services import delegation as delegation_module
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app


def _service(
    tmp_path: Path,
    *,
    config: RutherfordConfig | None = None,
    stdout: str = "the answer",
    exit_code: int = 0,
) -> DelegationService:
    runner = FakeProcessRunner(ProcessResult(exit_code=exit_code, stdout=stdout, stderr="boom" if exit_code else ""))
    return DelegationService(
        AdapterRegistry([FakeAdapter("fake")]),
        runner,
        config or RutherfordConfig(),
        load_roles(),
        ledger=RunLedger(tmp_path / "jobs"),
        clock=lambda: 1000.0,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="fake"), "prompt": "question"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


async def test_persist_true_writes_record_and_answer(tmp_path: Path) -> None:
    result = await _service(tmp_path).delegate(_req(persist=True))
    assert result.run_dir is not None
    run_dir = Path(result.run_dir)
    assert (run_dir / "artifacts" / "answer.md").read_text(encoding="utf-8") == "the answer"
    # Assert the persisted TOON as text (python-toon 0.1.3 cannot round-trip a quoted inline array;
    # see RunLedger). This verifies exactly what landed on disk.
    state = (run_dir / "state.toon").read_text(encoding="utf-8")
    assert "kind: delegate" in state
    assert "cli: fake" in state
    assert "ok: true" in state
    assert "schema_version: 1" in state
    assert "argv[" in state and "fake" in state  # the pinned invocation, for replay
    assert "created_at: 1000" in state
    assert "env:" not in state  # the child env (secrets) is never persisted


async def test_ephemeral_default_persists_nothing(tmp_path: Path) -> None:
    result = await _service(tmp_path).delegate(_req())  # persist None -> default ephemeral
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()


async def test_default_persistence_job_persists_without_an_explicit_flag(tmp_path: Path) -> None:
    result = await _service(tmp_path, config=RutherfordConfig(default_persistence="job")).delegate(_req())
    assert result.run_dir is not None


async def test_persist_false_overrides_a_job_default(tmp_path: Path) -> None:
    result = await _service(tmp_path, config=RutherfordConfig(default_persistence="job")).delegate(_req(persist=False))
    assert result.run_dir is None


async def test_failed_run_is_persisted_with_failed_status(tmp_path: Path) -> None:
    result = await _service(tmp_path, exit_code=1).delegate(_req(persist=True))
    assert not result.ok
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "ok: false" in state
    assert f"status: {JobStatus.FAILED.value}" in state
    assert "error_code: " in state


async def test_persist_failure_never_fails_the_delegation(tmp_path: Path) -> None:
    class BoomLedger(RunLedger):
        def write(self, record: RunRecord, *, answer: str, diff: str | None = None) -> Path:
            raise OSError("disk full")

    svc = DelegationService(
        AdapterRegistry([FakeAdapter("fake")]),
        FakeProcessRunner(ProcessResult(exit_code=0, stdout="the answer")),
        RutherfordConfig(),
        load_roles(),
        ledger=BoomLedger(tmp_path / "jobs"),
        clock=lambda: 1.0,
    )
    result = await svc.delegate(_req(persist=True))
    assert result.ok and result.text == "the answer"
    assert result.run_dir is None  # the write failed and was swallowed


async def test_no_ledger_wired_persists_nothing(tmp_path: Path) -> None:
    svc = DelegationService(
        AdapterRegistry([FakeAdapter("fake")]),
        FakeProcessRunner(ProcessResult(exit_code=0, stdout="x")),
        RutherfordConfig(),
        load_roles(),
        ledger=None,
    )
    result = await svc.delegate(_req(persist=True))
    assert result.run_dir is None


async def test_write_run_captures_changed_files_and_a_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delegation_module, "_git_changed_files", lambda wd, exclude=None: ["src/x.py"])
    monkeypatch.setattr(delegation_module, "_git_run", lambda wd, args: "+ added a line")
    result = await _service(tmp_path).delegate(
        _req(persist=True, safety_mode=SafetyMode.WRITE, working_dir=str(tmp_path), trust_workspace=True)
    )
    assert result.changed_files == ["src/x.py"]
    run_dir = Path(result.run_dir or "")
    assert "src/x.py" in (run_dir / "state.toon").read_text(encoding="utf-8")
    assert (run_dir / "artifacts" / "diff.md").is_file()


def test_git_changed_files_parses_porcelain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delegation_module,
        "_git_run",
        lambda wd, args: " M src/a.py\n?? new file.txt\nR  old.py -> renamed.py\n",
    )
    assert delegation_module._git_changed_files("/repo") == ["src/a.py", "new file.txt", "renamed.py"]


def test_git_changed_files_none_when_not_a_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delegation_module, "_git_run", lambda wd, args: None)
    assert delegation_module._git_changed_files("/repo") is None


def test_exclude_pathspec_excludes_a_nested_jobs_dir(tmp_path: Path) -> None:
    spec = delegation_module._exclude_pathspec(str(tmp_path), tmp_path / ".rutherford" / "jobs")
    assert spec == [":(exclude).rutherford/jobs"]


def test_exclude_pathspec_empty_when_jobs_dir_is_outside_the_tree(tmp_path: Path) -> None:
    spec = delegation_module._exclude_pathspec(str(tmp_path / "a"), tmp_path / "b" / "jobs")
    assert spec == []


def test_exclude_pathspec_empty_when_jobs_dir_equals_working_dir(tmp_path: Path) -> None:
    # Strict containment: jobs_dir == working_dir excludes nothing, rather than emitting `:(exclude).`
    # which would exclude the entire tree. Degrades safely (the jobs dir reappears in changed_files).
    assert delegation_module._exclude_pathspec(str(tmp_path), tmp_path) == []


class _SecretEnvAdapter(FakeAdapter):
    """A fake adapter whose invocation env carries a secret, to prove env never reaches the record."""

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        spec = super().build_invocation(req, ctx)
        return spec.model_copy(update={"env": {**spec.env, "API_KEY": "super-secret-xyz"}})


async def test_secret_in_child_env_never_reaches_the_record(tmp_path: Path) -> None:
    # Strong env-exclusion guard: the child env holds a real secret; the persisted record must not.
    svc = DelegationService(
        AdapterRegistry([_SecretEnvAdapter("fake")]),
        FakeProcessRunner(ProcessResult(exit_code=0, stdout="the answer")),
        RutherfordConfig(),
        load_roles(),
        ledger=RunLedger(tmp_path / "jobs"),
        clock=lambda: 1.0,
    )
    result = await svc.delegate(_req(persist=True))
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "super-secret-xyz" not in state
    assert "API_KEY" not in state


async def test_guard_failure_is_not_persisted(tmp_path: Path) -> None:
    # A run refused by an up-front guard (binary not installed) returns before the persist hook, so
    # only post-launch outcomes are recorded.
    svc = DelegationService(
        AdapterRegistry([FakeAdapter("fake", installed=False)]),
        FakeProcessRunner(ProcessResult(exit_code=0, stdout="x")),
        RutherfordConfig(),
        load_roles(),
        ledger=RunLedger(tmp_path / "jobs"),
        clock=lambda: 1.0,
    )
    result = await svc.delegate(_req(persist=True))
    assert not result.ok
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()


async def test_consensus_voices_do_not_persist_even_with_job_default(tmp_path: Path) -> None:
    # The panel footgun: with default_persistence="job", a consensus must not scatter orphan per-voice
    # records (panel-level linkage ships next slice), so each voice runs with persist=False.
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs"), default_persistence="job"),
    )
    await app.consensus.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    jobs = tmp_path / "jobs"
    assert not jobs.exists() or not any(jobs.iterdir())


async def test_role_is_captured_in_the_record(tmp_path: Path) -> None:
    result = await _service(tmp_path).delegate(_req(persist=True, role="planner"))
    assert result.run_dir is not None
    assert "role: planner" in (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
