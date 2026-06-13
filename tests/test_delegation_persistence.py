# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for durable run persistence in the delegation service (F2), driven by fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import JobStatus, SafetyMode, Strategy
from rutherford.domain.models import (
    ConsensusRequest,
    DebateRequest,
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
from rutherford.services.persistence import PanelVoice, write_panel_record
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
        def write(
            self,
            record: RunRecord,
            *,
            answer: str,
            diff: str | None = None,
            extra_artifacts: dict[str, str] | None = None,
        ) -> Path:
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


async def test_write_run_captures_changed_files_delta_and_a_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clean before the run; the run creates src/x.py, so the recorded delta is exactly [src/x.py].
    snapshots = iter([[], ["src/x.py"]])  # before-snapshot, then after
    monkeypatch.setattr(delegation_module, "_git_changed_files", lambda wd, exclude=None: next(snapshots, []))
    monkeypatch.setattr(delegation_module, "_git_run", lambda wd, args: "+ added a line")
    result = await _service(tmp_path).delegate(
        _req(persist=True, safety_mode=SafetyMode.WRITE, working_dir=str(tmp_path), trust_workspace=True)
    )
    assert result.changed_files == ["src/x.py"]
    run_dir = Path(result.run_dir or "")
    assert "src/x.py" in (run_dir / "state.toon").read_text(encoding="utf-8")
    assert (run_dir / "artifacts" / "diff.md").is_file()


async def test_pre_existing_dirty_file_is_not_attributed_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # foo.py was already dirty before the run; the run adds bar.py. Only bar.py is the run's delta.
    snapshots = iter([["foo.py"], ["foo.py", "bar.py"]])
    monkeypatch.setattr(delegation_module, "_git_changed_files", lambda wd, exclude=None: next(snapshots, []))
    monkeypatch.setattr(delegation_module, "_git_run", lambda wd, args: "diff")
    result = await _service(tmp_path).delegate(
        _req(persist=True, safety_mode=SafetyMode.WRITE, working_dir=str(tmp_path), trust_workspace=True)
    )
    assert result.changed_files == ["bar.py"]


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


async def test_consensus_persists_a_parent_and_child_records(tmp_path: Path) -> None:
    # A persisted panel is a parent record (kind=consensus, linking child_run_ids) plus a child record
    # per voice, each linked back by parent_run_id. Driven by default_persistence="job".
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs"), default_persistence="job"),
    )
    result = await app.consensus.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    assert result.run_dir is not None
    parent = Path(result.run_dir)
    parent_state = (parent / "state.toon").read_text(encoding="utf-8")
    assert "kind: consensus" in parent_state
    assert "child_run_ids[2]" in parent_state  # both voices linked
    dirs = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir()]
    assert len(dirs) == 3  # parent + two children
    for child in (d for d in dirs if d.name != parent.name):
        assert f"parent_run_id: {parent.name}" in (child / "state.toon").read_text(encoding="utf-8")


async def test_ephemeral_consensus_persists_nothing(tmp_path: Path) -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),  # default ephemeral
    )
    result = await app.consensus.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()


async def test_strategy_consensus_persists_a_parent_record(tmp_path: Path) -> None:
    # The strategy path (an aggregated StrategyResult, not all-voices) persists the same parent + child
    # records. With no free-text synthesis, the parent's status is derived from the voices (succeeded) and
    # a voices.md inlines each voice so the panel is auditable without every child record.
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="approve")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b")], prompt="q", strategy=Strategy.MAJORITY, persist=True
        )
    )
    assert result.run_dir is not None
    parent = Path(result.run_dir)
    parent_state = (parent / "state.toon").read_text(encoding="utf-8")
    assert "kind: consensus" in parent_state
    assert "child_run_ids[2]" in parent_state
    assert f"status: {JobStatus.SUCCEEDED.value}" in parent_state  # derived from the answering voices
    voices_md = (parent / "artifacts" / "voices.md").read_text(encoding="utf-8")
    assert voices_md.count("\n## ") == 2  # one section per voice
    dirs = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir()]
    assert len(dirs) == 3  # parent + two children


async def test_all_voices_failed_persists_a_failed_parent(tmp_path: Path) -> None:
    # The parent status is derived from the voices, not assumed SUCCEEDED: when every voice fails the parent
    # is FAILED, and its voices.md still inlines each voice's error so the failed panel stays auditable.
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=1, stdout="", stderr="boom")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", persist=True)
    )
    assert result.run_dir is not None
    parent = Path(result.run_dir)
    assert f"status: {JobStatus.FAILED.value}" in (parent / "state.toon").read_text(encoding="utf-8")
    voices_md = (parent / "artifacts" / "voices.md").read_text(encoding="utf-8")
    assert "(failed)" in voices_md
    assert "boom" in voices_md  # the actual error text is inlined, not just a (failed) marker


async def test_expand_all_with_everything_skipped_persists_an_auditable_failed_parent(tmp_path: Path) -> None:
    # An auto-expanded panel where every adapter is skipped (here: not installed) still persists an honest
    # parent: status FAILED, no children, and a voices.md that inlines the skip reasons -- so the all-skipped
    # panel, which has no child records to walk to, still explains itself from the parent alone.
    app = make_app(
        adapters=[FakeAdapter("a", installed=False), FakeAdapter("b", installed=False)],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="x")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(ConsensusRequest(targets=[], expand_all=True, prompt="q", persist=True))
    assert result.run_dir is not None
    parent = Path(result.run_dir)
    assert f"status: {JobStatus.FAILED.value}" in (parent / "state.toon").read_text(encoding="utf-8")
    voices_md = (parent / "artifacts" / "voices.md").read_text(encoding="utf-8")
    assert "## Skipped" in voices_md
    assert "a: " in voices_md and "b: " in voices_md  # both skipped adapters named with their reason
    children = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir() and d.name != parent.name]
    assert children == []  # no voice ran, so the parent links no children


async def test_debate_persists_a_parent_with_transcript_and_children(tmp_path: Path) -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="my position")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.debate.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, synthesize=False, persist=True)
    )
    assert result.run_dir is not None
    parent = Path(result.run_dir)
    assert "kind: debate" in (parent / "state.toon").read_text(encoding="utf-8")
    transcript = (parent / "artifacts" / "transcript.md").read_text(encoding="utf-8")
    assert "Round 1" in transcript and "Round 2" in transcript
    dirs = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir()]
    assert len(dirs) == 5  # parent + 2 voices x 2 rounds


def test_notice_suggests_a_job_for_an_unpersisted_complex_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # a workspace with no .rutherford config
    app = make_app(config=RutherfordConfig(default_persistence="ephemeral"))
    app.setup_hint_emitted = True  # isolate the suggest-a-job hint from the first-run hint
    notice = app.persistence_notice(persisted=False, complex_run=True, external_tracking=False)
    assert notice is not None and "persist=true" in notice


def test_notice_suppressed_by_external_tracking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    app = make_app()
    app.setup_hint_emitted = True
    assert app.persistence_notice(persisted=False, complex_run=True, external_tracking=True) is None


def test_first_run_hint_fires_once_per_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .rutherford config dir
    app = make_app()
    first = app.persistence_notice(persisted=True, complex_run=False, external_tracking=False)
    assert first is not None and "ephemeral by default" in first
    assert app.persistence_notice(persisted=True, complex_run=False, external_tracking=False) is None


async def test_panel_persist_failure_is_swallowed(tmp_path: Path) -> None:
    # A panel parent write that fails for any reason (here a non-OSError) returns no run_dir rather than
    # failing the panel that already produced an answer -- the best-effort contract, broadened to Exception.
    class BoomLedger(RunLedger):
        def write(
            self,
            record: RunRecord,
            *,
            answer: str,
            diff: str | None = None,
            extra_artifacts: dict[str, str] | None = None,
        ) -> Path:
            raise RuntimeError("kaboom")  # not an OSError; the broadened catch must still swallow it

    out = write_panel_record(
        BoomLedger(tmp_path / "jobs"),
        run_id="parent",
        kind="consensus",
        prompt="q",
        clis=["a"],
        voices=[PanelVoice(label="a", ok=True, run_id="child", text="hi")],
        answer="ans",
        created_at=1.0,
        finished_at=2.0,
    )
    assert out is None


async def test_role_is_captured_in_the_record(tmp_path: Path) -> None:
    result = await _service(tmp_path).delegate(_req(persist=True, role="planner"))
    assert result.run_dir is not None
    assert "role: planner" in (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
