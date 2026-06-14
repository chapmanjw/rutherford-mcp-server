# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for durable run persistence (F2) over ACP: leaf delegate records and panel parent/child records.

Drives the real in-process fake ACP agent (a subprocess), so the persisted ``state.toon`` carries the
actual resolved launch ``argv`` and a real answer. The fake agent's launch argv has colon-bearing elements
(``sys.executable`` plus a path), which python-toon 0.1.x cannot round-trip inside an inline array, so a
real run's record is asserted by reading ``state.toon`` as text (the human/LLM-readable form, which carries
everything verbatim) -- the clean-record decode round-trip lives in ``test_ledger``. ``env`` is verified to
be ABSENT from every persisted record.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import ConsensusRequest, DebateRequest, DelegationRequest, Target
from rutherford.io.ledger import RunLedger
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService
from rutherford.services.persistence import PanelVoice, render_panel_voice_files, write_panel_record

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD, default_model="m1")
FAKE_B = AgentDescriptor("fake_b", "Fake B", _FAKE_CMD, provider="beta", default_model="m2")
# An agent that exits before the handshake, so its voice always fails (a skipped/failed child).
DEAD = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))


def _has_env_field(state: str) -> bool:
    """Whether ``state.toon`` carries an ``env`` FIELD (not the letters 'env' inside a venv path/value).

    A TOON field is ``<key>:`` or ``<key>[...]:`` at the start of a line, so this checks for a real ``env``
    key rather than a substring -- a launch argv legitimately contains ``.venv\\Scripts\\python.exe``.
    """
    return any(line.startswith(("env:", "env[")) for line in state.splitlines())


def _registry(extra: list[AgentDescriptor] | None = None) -> DescriptorRegistry:
    return DescriptorRegistry([FAKE, FAKE_B, *(extra or [])])


def _delegation(
    tmp_path: Path, *, config: RutherfordConfig | None = None, registry: DescriptorRegistry | None = None
) -> DelegationService:
    resolved = config or RutherfordConfig()
    return DelegationService(
        registry or _registry(), resolved, ledger=RunLedger(tmp_path / "jobs"), clock=lambda: 1000.0
    )


def _consensus(tmp_path: Path, *, config: RutherfordConfig | None = None) -> ConsensusService:
    resolved = config or RutherfordConfig()
    registry = _registry()
    ledger = RunLedger(tmp_path / "jobs")
    delegation = DelegationService(registry, resolved, ledger=ledger, clock=lambda: 1000.0)
    return ConsensusService(delegation, registry, resolved, ledger=ledger, clock=lambda: 1000.0)


def _debate(tmp_path: Path, *, config: RutherfordConfig | None = None) -> DebateService:
    resolved = config or RutherfordConfig()
    registry = _registry()
    ledger = RunLedger(tmp_path / "jobs")
    delegation = DelegationService(registry, resolved, ledger=ledger, clock=lambda: 1000.0)
    return DebateService(registry, resolved, delegation, ledger=ledger, clock=lambda: 1000.0)


# --- the leaf delegate record ------------------------------------------------


async def test_persist_true_writes_state_and_answer(tmp_path: Path) -> None:
    result = await _delegation(tmp_path).delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT)),
    )
    assert result.run_dir is None  # default config is ephemeral; nothing yet

    result = await _delegation(tmp_path).delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), persist=True
        ),
    )
    assert result.ok and "42" in result.text
    assert result.run_dir is not None
    run_dir = Path(result.run_dir)
    assert (run_dir / "artifacts" / "answer.md").read_text(encoding="utf-8").strip() == result.text
    state = (run_dir / "state.toon").read_text(encoding="utf-8")
    assert "kind: delegate" in state
    assert "cli: fake" in state
    assert "ok: true" in state
    assert "schema_version: 1" in state
    # The replay-complete inputs are in the record: the prompt, the cwd, and the resolved launch argv (the
    # fake agent's command, carried verbatim into state.toon).
    assert "prompt: what is 17 + 25?" in state
    assert "fake_acp_agent.py" in state  # the pinned launch argv survives the write
    # env is NEVER persisted -- the record has no env field at all (it can carry secrets). Check the field
    # key, not the substring (a venv path legitimately contains the letters "env").
    assert not _has_env_field(state)


async def test_persist_false_writes_nothing(tmp_path: Path) -> None:
    service = _delegation(tmp_path)
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", working_dir=str(REPO_ROOT), persist=False)
    )
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()  # the jobs dir is created lazily, only on a real write


async def test_default_persistence_job_persists_by_default(tmp_path: Path) -> None:
    config = RutherfordConfig(default_persistence="job")
    result = await _delegation(tmp_path, config=config).delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", working_dir=str(REPO_ROOT)),  # no explicit persist
    )
    assert result.run_dir is not None
    assert (Path(result.run_dir) / "state.toon").is_file()


async def test_explicit_persist_false_overrides_default_job(tmp_path: Path) -> None:
    config = RutherfordConfig(default_persistence="job")
    result = await _delegation(tmp_path, config=config).delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", working_dir=str(REPO_ROOT), persist=False),
    )
    assert result.run_dir is None


async def test_failed_run_is_persisted_with_failed_status(tmp_path: Path) -> None:
    # A run that REACHED execution but failed (the agent refused) is still recorded -- the corpus is
    # post-launch outcomes, success and runtime failure alike.
    result = await _delegation(tmp_path).delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="REFUSE", working_dir=str(REPO_ROOT), persist=True),
    )
    assert result.ok is False
    assert result.run_dir is not None
    state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "status: failed" in state
    assert "ok: false" in state


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout


def _git_repo(path: Path) -> None:
    """Init a git repo with one commit so a write-mode delegation can run in a detached worktree off HEAD."""
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")


async def test_persisted_write_mode_writes_diff_artifact(tmp_path: Path) -> None:
    # A write-mode delegation whose agent creates a file produces a sandbox diff; a persisted write run
    # captures it in artifacts/diff.md (incl. the created/untracked file) and records the changed file.
    work = tmp_path / "work"
    work.mkdir()
    _git_repo(work)
    config = RutherfordConfig(trusted_workspaces=[str(work)])
    # The jobs dir is OUTSIDE the working tree, so a persisted write never mixes its own bookkeeping in.
    service = DelegationService(_registry(), config, ledger=RunLedger(tmp_path / "jobs"), clock=lambda: 1000.0)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="WRITE=created.txt:from the agent",
            working_dir=str(work),
            safety_mode=SafetyMode.WRITE,
            trust_workspace=True,
            persist=True,
            timeout_s=30.0,
        )
    )
    assert result.ok is True, f"write delegation failed: {result.error}"
    assert result.run_dir is not None
    run_dir = Path(result.run_dir)
    diff_md = (run_dir / "artifacts" / "diff.md").read_text(encoding="utf-8")
    assert "```diff" in diff_md
    assert "created.txt" in diff_md  # the created (untracked) file is in the diff
    state = (run_dir / "state.toon").read_text(encoding="utf-8")
    assert "created.txt" in state  # changed_files recorded
    assert "safety_mode: write" in state


async def test_unknown_target_refusal_is_not_persisted(tmp_path: Path) -> None:
    # An up-front guard refusal (unknown agent) returns before the persist hook -- a pre-flight refusal is
    # not part of the kept corpus.
    result = await _delegation(tmp_path).delegate(
        DelegationRequest(target=Target(cli="nope"), prompt="hi", persist=True),
    )
    assert result.ok is False
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()


async def test_ledger_write_failure_degrades_without_failing_the_run(tmp_path: Path) -> None:
    # An unwritable jobs dir (a FILE where the dir should be) makes the ledger write raise; persistence is
    # best-effort, so the run keeps its answer and simply carries no run_dir.
    jobs_path = tmp_path / "jobs"
    jobs_path.write_text("not a directory", encoding="utf-8")  # mkdir under it will raise
    service = DelegationService(_registry(), RutherfordConfig(), ledger=RunLedger(jobs_path), clock=lambda: 1000.0)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), persist=True
        )
    )
    assert result.ok and "42" in result.text  # the run still succeeded
    assert result.run_dir is None  # but nothing persisted


# --- the consensus panel parent + child records ------------------------------


async def test_persisted_consensus_writes_parent_and_children(tmp_path: Path) -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        persist=True,
    )
    result = await _consensus(tmp_path).consensus(request)
    assert result.run_dir is not None
    parent_dir = Path(result.run_dir)
    parent_state = (parent_dir / "state.toon").read_text(encoding="utf-8")
    assert "kind: consensus" in parent_state
    # The parent links two child voice records by run id.
    jobs_root = tmp_path / "jobs"
    child_dirs = [d for d in jobs_root.iterdir() if d.is_dir() and d != parent_dir]
    assert len(child_dirs) == 2, f"expected 2 child records, found {len(child_dirs)}"
    for child in child_dirs:
        child_state = (child / "state.toon").read_text(encoding="utf-8")
        assert "kind: delegate" in child_state
        assert f"parent_run_id: {parent_dir.name}" in child_state
        assert not _has_env_field(child_state)
    # Per-voice artifacts under the parent, with the real answers.
    voice1 = (parent_dir / "artifacts" / "voices" / "voice-1.md").read_text(encoding="utf-8")
    voice2 = (parent_dir / "artifacts" / "voices" / "voice-2.md").read_text(encoding="utf-8")
    assert "42" in voice1 and "42" in voice2
    # The parent's answer.md exists (a placeholder when no synthesis ran).
    assert (parent_dir / "artifacts" / "answer.md").is_file()
    # The parent rolls up: it lists both child run ids and names both clis.
    for child in child_dirs:
        assert child.name in parent_state


async def test_persisted_consensus_rolls_up_status_and_skipped(tmp_path: Path) -> None:
    # An expand_all panel that includes a dead agent records the failed voice as a child and the parent
    # stays succeeded (a live voice answered); skipped.md is written for any auto-panel exclusion.
    config = RutherfordConfig(max_targets=2)
    registry = _registry([DEAD])
    ledger = RunLedger(tmp_path / "jobs")
    delegation = DelegationService(registry, config, ledger=ledger, clock=lambda: 1000.0)
    service = ConsensusService(delegation, registry, config, ledger=ledger, clock=lambda: 1000.0)
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="dead")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        persist=True,
    )
    result = await service.consensus(request)
    assert result.run_dir is not None
    parent_state = (Path(result.run_dir) / "state.toon").read_text(encoding="utf-8")
    # One voice answered, so the panel parent is succeeded even though one child failed.
    assert "status: succeeded" in parent_state


async def test_consensus_persist_false_writes_nothing(tmp_path: Path) -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake_b")],
        prompt="hi",
        working_dir=str(REPO_ROOT),
        persist=False,
    )
    result = await _consensus(tmp_path).consensus(request)
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()


# --- the debate panel parent + transcript ------------------------------------


async def test_persisted_debate_writes_parent_and_transcript(tmp_path: Path) -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        rounds=2,
        working_dir=str(REPO_ROOT),
        persist=True,
    )
    result = await _debate(tmp_path).debate(request)
    assert result.run_dir is not None
    parent_dir = Path(result.run_dir)
    parent_state = (parent_dir / "state.toon").read_text(encoding="utf-8")
    assert "kind: debate" in parent_state
    assert "rounds: 2" in parent_state  # the PanelInputs records the round count
    assert not _has_env_field(parent_state)
    # A debate drives turns over persistent sessions (not via delegate), so the parent carries the run via
    # the full transcript -- every turn is inlined, with the real answers.
    transcript = (parent_dir / "artifacts" / "transcript.md").read_text(encoding="utf-8")
    assert "Debate transcript" in transcript
    assert "Round 1" in transcript and "Round 2" in transcript
    assert "42" in transcript
    assert (parent_dir / "artifacts" / "answer.md").is_file()


async def test_debate_persist_false_writes_nothing(tmp_path: Path) -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake_b")],
        prompt="hi",
        rounds=1,
        working_dir=str(REPO_ROOT),
        persist=False,
    )
    result = await _debate(tmp_path).debate(request)
    assert result.run_dir is None
    assert not (tmp_path / "jobs").exists()


# --- the panel-record helpers (direct, so the rollups are covered) -----------


def test_write_panel_record_rolls_up_status_cost_and_changed_files(tmp_path: Path) -> None:
    from rutherford.domain.models import Cost

    ledger = RunLedger(tmp_path / "jobs")
    voices = [
        PanelVoice(
            label="fake",
            ok=True,
            run_id="child1",
            text="answer one",
            cost=Cost(input_tokens=10, output_tokens=5),
            changed_files=("a.py",),
        ),
        PanelVoice(label="fake_b", ok=False, run_id="child2", error="boom", changed_files=("a.py", "b.py")),
    ]
    run_dir = write_panel_record(
        ledger,
        run_id="parent1",
        kind="consensus",
        prompt="q",
        clis=["fake", "fake_b"],
        voices=voices,
        answer="the synthesis",
        created_at=1000.0,
        finished_at=1005.0,
    )
    assert run_dir is not None
    state = (Path(run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "status: succeeded" in state  # one voice answered
    assert "child1" in state and "child2" in state  # both child run ids linked
    # The changed-file union is de-duplicated in first-seen order.
    assert "a.py" in state and "b.py" in state
    assert (Path(run_dir) / "artifacts" / "answer.md").read_text(encoding="utf-8") == "the synthesis"


def test_write_panel_record_all_failed_is_failed(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    voices = [PanelVoice(label="fake", ok=False, error="down"), PanelVoice(label="fake_b", ok=False, error="down")]
    run_dir = write_panel_record(
        ledger,
        run_id="p2",
        kind="consensus",
        prompt="q",
        clis=["fake", "fake_b"],
        voices=voices,
        answer="",
        created_at=1000.0,
        finished_at=1001.0,
    )
    assert run_dir is not None
    state = (Path(run_dir) / "state.toon").read_text(encoding="utf-8")
    assert "status: failed" in state
    assert "ok: false" in state  # ok tracks the derived status, not the RunRecord default


def test_write_panel_record_degrades_on_bad_jobs_dir(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs"
    jobs_path.write_text("not a dir", encoding="utf-8")
    ledger = RunLedger(jobs_path)
    run_dir = write_panel_record(
        ledger,
        run_id="p3",
        kind="consensus",
        prompt="q",
        clis=["fake"],
        voices=[PanelVoice(label="fake", ok=True, text="x")],
        answer="x",
        created_at=1000.0,
        finished_at=1001.0,
    )
    assert run_dir is None  # best-effort: a bad write returns None rather than raising


# --- the jobs-dir resolution (context wiring) --------------------------------


def test_resolve_jobs_dir_defaults_under_cwd() -> None:
    from rutherford.context import _resolve_jobs_dir

    resolved = _resolve_jobs_dir(RutherfordConfig())  # jobs_dir unset
    assert resolved == Path.cwd() / ".rutherford" / "jobs"


def test_resolve_jobs_dir_honors_configured_path(tmp_path: Path) -> None:
    from rutherford.context import _resolve_jobs_dir

    resolved = _resolve_jobs_dir(RutherfordConfig(jobs_dir=str(tmp_path / "custom")))
    assert resolved == tmp_path / "custom"


def test_build_app_context_wires_a_ledger() -> None:
    # The context builds a RunLedger and injects it into the delegation/consensus/debate services so a
    # persist=true call has somewhere to write.
    from rutherford.context import build_app_context

    app = build_app_context(config=RutherfordConfig(), descriptors=_registry())
    assert app.delegation._ledger is not None
    assert app.consensus._ledger is not None
    assert app.debate._ledger is not None


def test_render_panel_voice_files_layout() -> None:
    voices = [
        PanelVoice(label="fake", ok=True, run_id="c1", text="42"),
        PanelVoice(label="dead", ok=False, error="handshake failed"),
        PanelVoice(label="slow", ok=False, error="cut", partial="partial-so-far", session_id="sess-9"),
    ]
    artifacts = render_panel_voice_files(voices, skipped=[("kimi", "not installed")])
    assert "voices/voice-1.md" in artifacts
    assert "42" in artifacts["voices/voice-1.md"]
    assert "_run: c1_" in artifacts["voices/voice-1.md"]
    assert "(failed)" in artifacts["voices/voice-2.md"]
    assert "handshake failed" in artifacts["voices/voice-2.md"]
    # A cut voice keeps its partial and records its resume handle (it has no child record of its own).
    assert "partial-so-far" in artifacts["voices/voice-3.md"]
    assert "_session: sess-9_" in artifacts["voices/voice-3.md"]
    assert "voices/skipped.md" in artifacts
    assert "kimi: not installed" in artifacts["voices/skipped.md"]
