# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for durable run persistence in the delegation service (F2), driven by fakes."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import Effort, JobStatus, SafetyMode, Stance, Strategy
from rutherford.domain.models import (
    ConsensusRequest,
    Cost,
    DebateRequest,
    DelegationRequest,
    DelegationResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    RunRecord,
    RunRollup,
    Target,
)
from rutherford.io.ledger import RunLedger
from rutherford.services import debate as debate_module
from rutherford.services import delegation as delegation_module
from rutherford.services.delegation import DelegationService
from rutherford.services.persistence import PanelVoice, render_panel_voice_files, write_panel_record
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


def test_git_diff_includes_created_untracked_file_contents(tmp_path: Path) -> None:
    # Real-git (not mocked): git diff HEAD omits untracked/created files, but a write run that CREATES a
    # file must capture its contents in diff.md (1-E). This would fail with the old plain `git diff HEAD`.
    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "init")
    (tmp_path / "tracked.txt").write_text("original\nmodified\n", encoding="utf-8")  # modify a tracked file
    (tmp_path / "created.py").write_text("print('a created file')\n", encoding="utf-8")  # create a new file
    diff = delegation_module._git_diff(str(tmp_path))
    assert diff is not None
    assert "modified" in diff  # the tracked change (git diff HEAD)
    assert "created.py" in diff and "print('a created file')" in diff  # the created file's contents (the fix)


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


async def test_consensus_parent_records_the_panel_orchestration_config(tmp_path: Path) -> None:
    # 1-D (panel parent): the parent snapshots the panel's resolved config -- the seat roster (incl.
    # per-target model) and the aggregation strategy -- so the panel, not just each voice, replays from
    # the parent's state.toon. These semantics live on no child record.
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="approve")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b", model="m1")],
            prompt="q",
            strategy=Strategy.MAJORITY,
            persist=True,
        )
    )
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "panel:" in state
    assert "strategy: majority" in state
    assert "m1" in state  # the per-target model is captured in the resolved roster


async def test_consensus_parent_snapshots_the_resolved_synthesize(tmp_path: Path) -> None:
    # 1-D: the panel config snapshot must be the RESOLVED orchestration config. With synthesize_default=true
    # and the request omitting synthesize, the parent must record synthesize=true (the resolved behavior),
    # not the unresolved request value.
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs"), synthesize_default=True),
    )
    result = await app.consensus.consensus(  # synthesize omitted -> resolves to the config default (true)
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", persist=True)
    )
    assert result.run_dir is not None
    assert "synthesize: true" in (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")


async def test_consensus_parent_roster_records_parallel_stances(tmp_path: Path) -> None:
    # The roster must capture per-seat steering supplied via the parallel stances array, not only via a
    # Target.stance, so a stance-steered panel replays who argued which side from the parent (1-D).
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b")],
            prompt="q",
            stances=[Stance.FOR, Stance.AGAINST],
            persist=True,
        )
    )
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    # The roster is a TOON table (targets[..]{cli,stance}: rows), so the stances appear as table rows.
    assert "stance" in state  # the roster column header
    assert "a,for" in state and "b,against" in state  # each seat's resolved stance, in order


async def test_debate_parent_records_rounds_in_the_panel_config(tmp_path: Path) -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="my position")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.debate.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, synthesize=False, persist=True)
    )
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "panel:" in state and "rounds: 2" in state
    # 2-I: each seat's resume handle is recorded in the parent roster (state.toon), matching consensus,
    # so a later continuation can resume the debate's seats from the parent record.
    assert "fake-session" in state


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
    # records. The parent's status is derived from the voices (succeeded) and one voices/voice-N.md per
    # voice (the locked 1-layout) makes the panel auditable without every child record.
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
    assert "ok: true" in parent_state  # ok tracks the derived status
    voices_dir = parent / "artifacts" / "voices"
    # The clean per-voice result files (the locked 1-layout), excluding the F8a ``.live.md`` raw-stream
    # tee files (2-G), which are a separate incremental artifact.
    voice_files = sorted(p.name for p in voices_dir.glob("voice-*.md") if not p.name.endswith(".live.md"))
    assert voice_files == ["voice-1.md", "voice-2.md"]  # one clean file per voice, the locked layout
    assert "approve" in (voices_dir / "voice-1.md").read_text(encoding="utf-8")
    dirs = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir()]
    assert len(dirs) == 3  # parent + two children


async def test_all_voices_failed_persists_a_failed_parent(tmp_path: Path) -> None:
    # The parent status is derived from the voices, not assumed SUCCEEDED: when every voice fails the parent
    # is FAILED, and each voices/voice-N.md still inlines that voice's error so the failed panel stays auditable.
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
    parent_state = (parent / "state.toon").read_text(encoding="utf-8")
    assert f"status: {JobStatus.FAILED.value}" in parent_state
    assert "ok: false" in parent_state  # ok must track the derived status, not default to true
    voice_1 = (parent / "artifacts" / "voices" / "voice-1.md").read_text(encoding="utf-8")
    assert "(failed)" in voice_1
    assert "boom" in voice_1  # the actual error text is inlined, not just a (failed) marker


async def test_expand_all_with_everything_skipped_persists_an_auditable_failed_parent(tmp_path: Path) -> None:
    # An auto-expanded panel where every adapter is skipped (here: not installed) still persists an honest
    # parent: status FAILED, no children, and a voices/skipped.md that inlines the skip reasons -- so the
    # all-skipped panel, which has no child records to walk to, still explains itself from the parent alone.
    app = make_app(
        adapters=[FakeAdapter("a", installed=False), FakeAdapter("b", installed=False)],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="x")),
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(ConsensusRequest(targets=[], expand_all=True, prompt="q", persist=True))
    assert result.run_dir is not None
    parent = Path(result.run_dir)
    parent_state = (parent / "state.toon").read_text(encoding="utf-8")
    assert f"status: {JobStatus.FAILED.value}" in parent_state
    assert "ok: false" in parent_state  # no voice answered -> failed and not-ok
    skipped_md = (parent / "artifacts" / "voices" / "skipped.md").read_text(encoding="utf-8")
    assert "Skipped" in skipped_md
    assert "a: " in skipped_md and "b: " in skipped_md  # both skipped adapters named with their reason
    children = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir() and d.name != parent.name]
    assert children == []  # no voice ran, so the parent links no children


def test_debate_changed_files_flow_to_the_panel_parent(tmp_path: Path) -> None:
    # 1-D: a write-mode debate's parent must roll up the turns' changed files (like consensus). Guard the
    # full chain result -> contribution -> PanelVoice; before the fix DebateContribution dropped them, so
    # the debate parent's changed-file union was always empty.
    voice = debate_module._Voice(index=0, target=Target(cli="a"), label="a", seat_id="0:a", stance=None, role=None)
    result = DelegationResult(target=Target(cli="a"), ok=True, text="done", changed_files=["src/new.py"])
    contribution = debate_module._to_contribution(voice, 1, result)
    assert contribution.changed_files == ["src/new.py"]  # carried from the result
    assert debate_module._panel_voice(contribution).changed_files == ("src/new.py",)  # and into the rollup input


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


def test_first_run_hint_suppressed_once_workspace_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop closes: once a workspace .rutherford/ exists (setup scope=project wrote config there, or a
    # job was persisted), the first-run hint no longer fires.
    (tmp_path / ".rutherford").mkdir()
    monkeypatch.chdir(tmp_path)
    app = make_app()
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


async def test_requested_model_is_captured_on_the_leaf_record(tmp_path: Path) -> None:
    # 1-D: the leaf record keeps the requested model (pre-fallback) alongside the resolved one.
    result = await _service(tmp_path).delegate(_req(persist=True, target=Target(cli="fake", model="m1")))
    assert result.run_dir is not None
    assert "requested_model: m1" in (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")


def test_render_panel_voice_files_one_per_voice_plus_skipped() -> None:
    # The locked 1-layout: one voices/voice-N.md per voice, and a voices/skipped.md for skipped adapters.
    voices = [
        PanelVoice(label="a", ok=True, text="hi", run_id="c1"),
        PanelVoice(label="b", ok=False, error="boom"),
    ]
    files = render_panel_voice_files(voices, skipped=[("c", "not installed")])
    assert set(files) == {"voices/voice-1.md", "voices/voice-2.md", "voices/skipped.md"}
    assert "hi" in files["voices/voice-1.md"] and "_run: c1_" in files["voices/voice-1.md"]
    assert "(failed)" in files["voices/voice-2.md"] and "boom" in files["voices/voice-2.md"]
    assert "c: not installed" in files["voices/skipped.md"]


def test_render_panel_voice_files_no_skipped_section_when_none() -> None:
    files = render_panel_voice_files([PanelVoice(label="a", ok=True, text="hi")])
    assert set(files) == {"voices/voice-1.md"}  # no skipped.md when nothing was skipped


def test_panel_parent_rolls_up_cost_files_and_request_metadata(tmp_path: Path) -> None:
    # 1-D: the panel parent is not a thin link record -- it rolls up duration, the request's
    # safety_mode/files/role, the deduped union of the voices' changed files, and the summed cost.
    ledger = RunLedger(tmp_path / "jobs")
    voices = [
        PanelVoice(
            label="a", ok=True, run_id="c1", text="hi", cost=Cost(usd=0.5, input_tokens=10), changed_files=("x.py",)
        ),
        PanelVoice(
            label="b",
            ok=False,
            run_id="c2",
            error="boom",
            cost=Cost(usd=0.25, input_tokens=4),
            changed_files=("x.py", "y.py"),
        ),
    ]
    out = write_panel_record(
        ledger,
        run_id="parent",
        kind="consensus",
        prompt="q",
        clis=["a", "b"],
        voices=voices,
        answer="ans",
        created_at=1000.0,
        finished_at=1002.5,
        safety_mode=SafetyMode.WRITE,
        cwd="/work/repo",
        files=["in.py"],
        role="reviewer",
        extra_artifacts=render_panel_voice_files(voices),
    )
    assert out is not None
    state = (Path(out) / "state.toon").read_text(encoding="utf-8")
    assert "role: reviewer" in state
    assert "cwd: /work/repo" in state  # the parent captures cwd for replay (1-D)
    assert "safety_mode: write" in state
    assert "status: succeeded" in state  # any voice ok -> succeeded
    assert "duration_s: 2.5" in state
    assert "usd: 0.75" in state  # 0.5 + 0.25 summed
    assert "input_tokens: 14" in state  # 10 + 4 summed
    assert "x.py" in state and "y.py" in state  # changed-file union, deduped
    assert (Path(out) / "artifacts" / "voices" / "voice-1.md").is_file()
    assert (Path(out) / "artifacts" / "voices" / "voice-2.md").is_file()


def test_panel_cost_rollup_is_none_when_no_voice_reported_cost(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    out = write_panel_record(
        ledger,
        run_id="parent",
        kind="consensus",
        prompt="q",
        clis=["a"],
        voices=[PanelVoice(label="a", ok=True, run_id="c1", text="hi")],
        answer="ans",
        created_at=1.0,
        finished_at=2.0,
    )
    assert out is not None
    assert "cost:" not in (Path(out) / "state.toon").read_text(encoding="utf-8")  # no misleading zero cost


# --- F8a: the time-budget rollup / stop_reason / partial flow into the persisted record ----------


def test_panel_parent_records_the_budget_rollup_and_stop_reason(tmp_path: Path) -> None:
    # F8a: a panel harvested at its time budget records stop_reason=budget plus the rollup (counts,
    # quorum, effort) on the parent state.toon, and mirrors the rollup's effort onto the record fields.
    ledger = RunLedger(tmp_path / "jobs")
    rollup = RunRollup(
        stop_reason="budget",
        requested=3,
        answered=2,
        cut=1,
        usable=2,
        quorum_met=True,
        elapsed_s=12.0,
        time_budget_s=10.0,
        effort_requested=Effort.HIGH,
        effort_applied=Effort.HIGH,
    )
    out = write_panel_record(
        ledger,
        run_id="parent",
        kind="consensus",
        prompt="q",
        clis=["a", "b"],
        voices=[PanelVoice(label="a", ok=True, run_id="c1", text="hi")],
        answer="ans",
        created_at=1.0,
        finished_at=13.0,
        stop_reason="budget",
        rollup=rollup,
    )
    assert out is not None
    state = (Path(out) / "state.toon").read_text(encoding="utf-8")
    assert "stop_reason: budget" in state
    assert "rollup:" in state
    assert "cut: 1" in state and "usable: 2" in state
    assert "requested_effort: high" in state  # mirrored from the rollup onto the record fields
    assert "effort_applied: high" in state


def test_render_panel_voice_files_surfaces_a_cut_voices_partial() -> None:
    # F8a 2-G: a voice cut at the deadline has no final answer, but the partial it streamed before the
    # cut is preserved in its voice-N.md so the in-flight work lands in the job artifacts.
    voices = [
        PanelVoice(label="a", ok=True, text="full answer", run_id="c1"),
        PanelVoice(label="b", ok=False, error="cut at the panel time-budget deadline", partial="draft so far"),
    ]
    files = render_panel_voice_files(voices)
    assert "full answer" in files["voices/voice-1.md"]
    cut = files["voices/voice-2.md"]
    assert "(failed)" in cut
    assert "Partial output (harvested at the cut)" in cut
    assert "draft so far" in cut


class _BudgetPersistRunner:
    """A runner with per-cli delays that streams partial lines before the delay, for an end-to-end test.

    Drives a real time-budget harvest through the persisting consensus service so the parent record and
    the cut voice's artifact can be asserted on disk.
    """

    def __init__(self, delays: dict[str, float], partials: dict[str, list[str]] | None = None) -> None:
        self.delays = delays
        self.partials = partials or {}

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        cli = spec.argv[0]
        for line in self.partials.get(cli, []):
            if on_stdout is not None:
                on_stdout(line)
        await asyncio.sleep(self.delays.get(cli, 0.0))
        return ProcessResult(exit_code=0, stdout=f"{cli} answered")


async def test_consensus_budget_harvest_persists_rollup_and_partial(tmp_path: Path) -> None:
    # The full F8a payoff, persisted: a budget harvest writes the parent's rollup/stop_reason AND the cut
    # voice's pre-deadline partial into the job artifacts, so no in-flight effort is lost on a kept job.
    # The FakeAdapter is TEXT (supports_partial_output), so the slow voice's streamed partial is harvested
    # into a usable candidate answer and lands in its voice artifact as the answer.
    runner = _BudgetPersistRunner({"fast": 0.0, "slow": 5.0}, partials={"slow": ["thinking step one"]})
    app = make_app(
        adapters=[FakeAdapter("fast"), FakeAdapter("slow")],
        runner=runner,  # type: ignore[arg-type]
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", persist=True, time_budget_s=0.3)
    )
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "stop_reason: budget" in state
    assert "rollup:" in state
    # 2-I: the cut voice's resume handle (recovered from the harvested partial) is recorded structurally in
    # the parent state.toon roster, so a later continuation can read it (not only in the human artifact).
    assert "fake-session" in state
    # The cut voice ("slow") is voice 2; the work it streamed before the cut is preserved in its artifact,
    # along with its resume handle for a human reader.
    cut_artifact = (Path(result.run_dir) / "artifacts" / "voices" / "voice-2.md").read_text(encoding="utf-8")
    assert "thinking step one" in cut_artifact
    assert "_session: fake-session_" in cut_artifact  # the cut voice's resume handle is recorded


async def test_persisted_debate_tees_each_round_to_live_artifacts(tmp_path: Path) -> None:
    # F8a 2-G for debate: a persisted debate tees each round's turns into round-namespaced live artifacts as
    # they arrive, so a turn cut mid-round preserves its streamed stdout on disk independent of finalization.
    runner = _BudgetPersistRunner({"a": 0.0, "b": 5.0}, partials={"b": ["round one rebuttal so far"]})
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=runner,  # type: ignore[arg-type]
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.debate.debate(
        DebateRequest(
            targets=[Target(cli="a"), Target(cli="b")],
            prompt="q",
            rounds=2,
            synthesize=False,
            persist=True,
            time_budget_s=0.3,
        )
    )
    assert result.run_dir is not None
    live = (Path(result.run_dir) / "artifacts" / "voices" / "round-1-voice-2.live.md").read_text(encoding="utf-8")
    assert "round one rebuttal so far" in live  # the cut turn's in-flight stream, teed live and namespaced


async def test_persisted_panel_tees_voice_stdout_to_a_live_artifact(tmp_path: Path) -> None:
    # F8a 2-G (stream-to-job): a persisted panel tees each voice's stdout into a live artifact as it runs,
    # so the in-flight stream is on disk independent of finalization. The final snapshot lands a
    # voice-N.live.md carrying what the voice streamed (here the slow voice's pre-cut partial).
    runner = _BudgetPersistRunner(
        {"fast": 0.0, "slow": 5.0}, partials={"slow": ["streamed line one", "streamed line two"]}
    )
    app = make_app(
        adapters=[FakeAdapter("fast"), FakeAdapter("slow")],
        runner=runner,  # type: ignore[arg-type]
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")),
    )
    result = await app.consensus.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", persist=True, time_budget_s=0.3)
    )
    assert result.run_dir is not None
    live = (Path(result.run_dir) / "artifacts" / "voices" / "voice-2.live.md").read_text(encoding="utf-8")
    assert "streamed line one" in live and "streamed line two" in live
