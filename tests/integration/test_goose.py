# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration tests: drive the real ``goose acp`` agent over ACP (local only, run with -m integration).

These verify the full ACP-native stack -- delegate, consensus, and debate (persistent sessions) -- against
a real agent, not the fake one. Slow (real model calls); deselected by default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry, default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import Effort, SafetyMode, Strategy
from rutherford.domain.models import (
    ConsensusRequest,
    ConsensusResult,
    DebateRequest,
    DelegationRequest,
    DelegationResult,
    StrategyResult,
    Target,
)
from rutherford.io.ledger import RunLedger, read_record
from rutherford.io.serialize import decode
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService
from rutherford.tools.plan import plan_tool
from rutherford.tools.review import review_tool

pytestmark = pytest.mark.integration

_PROMPT = "Reply with ONLY the number, nothing else: what is 17 + 25?"


async def test_goose_delegate_turn() -> None:
    goose = default_registry().get("goose")
    result = await run_acp_turn(
        goose, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=120.0
    )
    assert result.ok is True, f"goose failed: {result.error}"
    assert "42" in result.text
    assert result.session_id is not None


@pytest.mark.parametrize("agent_id", ["goose", "vibe", "junie", "opencode", "cline"])
async def test_working_agent_answers(agent_id: str) -> None:
    """The agents that drive cleanly over ACP-stdio on this machine each answer a trivial prompt.

    cline answers only with Cline's own service auth -- a ChatGPT-subscription or OpenRouter provider set in
    the desktop app does not reach the headless `--acp` path (it returns an empty turn).
    """
    descriptor = default_registry().get(agent_id)
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=120.0
    )
    assert result.ok is True, f"{agent_id} failed: {result.error}"
    assert "42" in result.text


@pytest.mark.parametrize("agent_id", ["codex", "claude_code"])
async def test_official_adapter_answers(agent_id: str) -> None:
    """The official Zed adapters drive their CLI over ACP using the existing CLI login (no API key).

    ``codex`` (codex-acp) reuses the ChatGPT login and ``claude_code`` (claude-agent-acp) reuses the Claude
    Code login; both stream an answer end to end (receipt 11-official-adapters-auth-test.md). A longer budget
    than the other agents because the first turn also negotiates the underlying CLI's auth.
    """
    descriptor = default_registry().get(agent_id)
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=180.0
    )
    assert result.ok is True, f"{agent_id} failed: {result.error}"
    assert "42" in result.text


@pytest.mark.parametrize("agent_id", ["copilot", "qwen", "droid", "cursor", "kiro", "pi"])
async def test_second_wave_agent_answers(agent_id: str) -> None:
    """The second/third wave (probed live, receipts 12/13) each drive over ACP with the existing CLI auth.

    copilot (GitHub Copilot plan), qwen (~/.qwen), droid (Factory -- separate billing), cursor (Cursor
    subscription; the `acp` subcommand is hidden from --help), kiro (kiro-cli, not the IDE-launcher `kiro`),
    pi (the pi-acp wrapper over `pi --mode rpc`). Each answers a trivial prompt end to end.

    Not parametrized here, on purpose:
    - hermes: registered and functions over ACP (probe answers in ~7-9s), but the Nous endpoint latency
      swings from seconds to >190s, so it cannot satisfy a bounded-timeout assertion -- check it with
      ``doctor`` live instead.
    - kilo: its Auto Kilo Free Gateway works only in the interactive TUI, not a headless spawn; it needs a
      real ``kilo auth`` credential before a headless turn completes.
    """
    descriptor = default_registry().get(agent_id)
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=180.0
    )
    assert result.ok is True, f"{agent_id} failed: {result.error}"
    assert "42" in result.text


def _consensus_service(config: RutherfordConfig | None = None) -> ConsensusService:
    resolved = config or RutherfordConfig()
    registry = default_registry()
    return ConsensusService(DelegationService(registry, resolved), registry, resolved)


async def test_goose_consensus_two_voices() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="goose"), Target(cli="goose")], prompt=_PROMPT, working_dir=str(Path.cwd()), timeout_s=120.0
    )
    result = await _consensus_service().consensus(request)
    assert isinstance(result, ConsensusResult)  # the all-voices path returns every voice
    voices = result.voices
    assert len(voices) == 2
    assert any(voice.ok for voice in voices), f"all voices failed: {[v.error for v in voices]}"
    assert all("42" in voice.text for voice in voices if voice.ok)


async def test_goose_consensus_topology_populated_live() -> None:
    """A real two-goose consensus carries a populated Topology with a non-trivial observed agent floor (N1).

    Drives the full N1 stack against a real agent: the psutil sampler walks each goose's process tree while
    it runs, the panel sums the realized delegations, and the result reports declared/realized/observed.
    A real goose agent spawns at least itself, so ``observed_peak_agents >= 1`` is the floor we can assert.
    """
    request = ConsensusRequest(
        targets=[Target(cli="goose"), Target(cli="goose")], prompt=_PROMPT, working_dir=str(Path.cwd()), timeout_s=120.0
    )
    result = await _consensus_service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.topology is not None, "the consensus result carried no Topology"
    topology = result.topology
    assert topology.declared == 2
    assert topology.realized_delegations == 2  # one subprocess delegation per voice, no fallback
    assert topology.observed_peak_agents is not None and topology.observed_peak_agents >= 1, (
        f"expected a live observed floor >= 1, got {topology.observed_peak_agents}"
    )
    assert topology.over_cap is False


async def test_goose_consensus_persists_to_disk_live(tmp_path: Path) -> None:
    """A real two-goose consensus with persist=True lands a parent + two child records and voice artifacts (F2).

    Drives the full F2 stack against a real agent: each voice persists its own leaf record, and the panel
    writes a parent state.json linking both children plus a voices/voice-N.md per voice with the real answers.
    """
    config = RutherfordConfig()
    registry = default_registry()
    ledger = RunLedger(tmp_path / "jobs")
    delegation = DelegationService(registry, config, ledger=ledger)
    service = ConsensusService(delegation, registry, config, ledger=ledger)
    request = ConsensusRequest(
        targets=[Target(cli="goose"), Target(cli="goose")],
        prompt=_PROMPT,
        working_dir=str(Path.cwd()),
        timeout_s=120.0,
        persist=True,
    )
    result = await service.consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.run_dir is not None, "the persisted consensus carried no run_dir"
    parent_dir = Path(result.run_dir)
    parent = read_record(parent_dir)  # state.json round-trips through the reader
    assert parent.kind == "consensus"
    assert len(parent.child_run_ids) == 2, "the parent did not link two child records"
    # Two child leaf records exist on disk.
    child_dirs = [d for d in (tmp_path / "jobs").iterdir() if d.is_dir() and d != parent_dir]
    assert len(child_dirs) == 2
    # The per-voice artifacts carry the real answers.
    voice1 = (parent_dir / "artifacts" / "voices" / "voice-1.md").read_text(encoding="utf-8")
    voice2 = (parent_dir / "artifacts" / "voices" / "voice-2.md").read_text(encoding="utf-8")
    assert "42" in voice1 and "42" in voice2, f"voice artifacts missing the answer:\n{voice1}\n---\n{voice2}"
    assert (parent_dir / "artifacts" / "answer.md").is_file()


async def test_fallback_chain_recovers_a_spawn_fail_on_a_real_agent() -> None:
    """A real spawn-fail SAFE failure falls back to a live goose, recording the chain and the real answer (F7).

    The primary ``broken`` agent is configured with a command that does not exist, so its turn fails
    pre-prompt with ``ACP_SPAWN_FAILED`` / re-execution-SAFE; the fallback chain then drives the REAL goose
    agent, which answers. Proves the whole reliability path end to end against a real agent: the SAFE gate
    lets the fallback fire, ``fallback_chain`` shows the failed primary, ``delegation_call_count`` counts both
    attempts, and the final answer is goose's "42".
    """
    broken = AgentDescriptor("broken", "Broken", ("this-binary-does-not-exist-xyz123",))
    registry = DescriptorRegistry([broken, default_registry().get("goose")])
    service = DelegationService(registry, RutherfordConfig())
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="broken"),
            prompt=_PROMPT,
            working_dir=str(Path.cwd()),
            timeout_s=120.0,
            fallback=[Target(cli="goose")],
        )
    )
    assert result.ok is True, f"fallback to goose failed: {result.error}"
    assert "42" in result.text
    assert result.target.cli == "goose"  # goose is whoever finally answered
    assert result.fallback_chain == ["broken"]  # the failed primary leads the chain
    assert result.delegation_call_count == 2  # the broken primary attempt + goose


_VERDICT_PROMPT = (
    "Is 17 + 25 equal to 42? Answer with a final line that is exactly 'VERDICT: yes' if it is equal, "
    "or exactly 'VERDICT: no' if it is not."
)


async def test_goose_consensus_majority_strategy_live() -> None:
    """A real majority-strategy consensus across two goose voices on a crisp yes/no verdict prompt.

    Drives the full aggregating path against a real agent: each voice answers with a VERDICT line, the
    strategy extracts each verdict and reduces the panel to one outcome. Asserts a real StrategyResult
    with a sensible outcome (a majority on the true proposition, or no_majority/split if a voice drifts).
    """
    request = ConsensusRequest(
        targets=[Target(cli="goose"), Target(cli="goose")],
        prompt=_VERDICT_PROMPT,
        strategy=Strategy.MAJORITY,
        working_dir=str(Path.cwd()),
        timeout_s=120.0,
    )
    result = await _consensus_service().consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.strategy is Strategy.MAJORITY
    assert len(result.voices) == 2
    parsed = [voice for voice in result.voices if voice.verdict is not None]
    assert parsed, f"no voice produced a parseable verdict: {[(v.label, v.text[:80]) for v in result.voices]}"
    assert result.outcome in {"majority", "no_majority", "no_quorum"}
    if result.outcome == "majority":
        assert result.decision == "yes"


async def test_goose_debate_persistent_sessions() -> None:
    config = RutherfordConfig()
    registry = default_registry()
    service = DebateService(registry, config, DelegationService(registry, config))
    request = DebateRequest(
        targets=[Target(cli="goose"), Target(cli="goose")],
        prompt=_PROMPT,
        rounds=2,
        working_dir=str(Path.cwd()),
        timeout_s=120.0,
    )
    result = await service.debate(request)
    assert len(result.rounds) >= 1
    assert any(contribution.ok for round_ in result.rounds for contribution in round_.contributions)


async def test_goose_session_resume_recalls_prior_context() -> None:
    """A second delegate that passes the first result's ``session_id`` resumes the SAME goose conversation over
    ACP ``session/load``: goose recalls a codeword set in the first turn -- the live proof of session resume.

    Each delegate spawns a fresh goose process, so call 2 recalling the codeword means goose genuinely reloaded
    the prior session from its own persistence, driven by Rutherford's resume (not a fresh session).
    """
    service = DelegationService(default_registry(), RutherfordConfig())
    cwd = str(Path.cwd())
    established = await service.delegate(
        DelegationRequest(
            target=Target(cli="goose"),
            prompt="Remember this codeword for later: BANANA-7. Reply with just: OK",
            working_dir=cwd,
            timeout_s=120.0,
        )
    )
    assert established.ok is True, f"establishing the session failed: {established.error}"
    assert established.session_id is not None
    resumed = await service.delegate(
        DelegationRequest(
            target=Target(cli="goose"),
            prompt="What was the codeword I told you to remember? Reply with just the codeword.",
            working_dir=cwd,
            timeout_s=120.0,
            session_id=established.session_id,
        )
    )
    assert resumed.ok is True, f"resuming the session failed: {resumed.error}"
    assert resumed.session_id == established.session_id  # the SAME session, resumed -- not a fresh one
    assert "BANANA-7" in resumed.text, f"the resumed session did not recall the codeword: {resumed.text!r}"


# --- F8a: time budget + effort against real agents ---------------------------

#: A prompt that makes a real agent think for a while, so a tight panel deadline reliably catches a voice
#: in flight. Open-ended on purpose -- the goal is a long turn, not a crisp answer.
_SLOW_PROMPT = (
    "Think step by step and write a thorough, multi-paragraph analysis (at least 8 paragraphs): compare "
    "the trade-offs of monolith vs microservice architectures across team size, latency, deployment, data "
    "consistency, and operational cost. Be exhaustive."
)


async def test_goose_consensus_time_budget_harvest() -> None:
    """A real two-voice goose consensus under a tight time budget forces a harvest (F8a).

    Both voices get a deliberately long prompt and the panel deadline is short, so at least one voice is in
    flight at the deadline and is cut. Asserts ``stop_reason="budget"`` and a rollup recording the cut -- the
    live proof the wall-clock harvest works end to end against a real agent, not just the fake.
    """
    request = ConsensusRequest(
        targets=[Target(cli="goose"), Target(cli="goose")],
        prompt=_SLOW_PROMPT,
        working_dir=str(Path.cwd()),
        timeout_s=120.0,  # the per-turn fault budget, far longer than the panel deadline below
        time_budget_s=6.0,  # the whole-panel wall-clock deadline -- shorter than the long turn takes
    )
    result = await _consensus_service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.stop_reason == "budget", f"expected a harvest, got {result.stop_reason!r}"
    assert result.rollup is not None
    assert result.rollup.stop_reason == "budget"
    assert result.rollup.cut >= 1, f"expected at least one cut voice, rollup={result.rollup.model_dump()}"
    assert result.rollup.time_budget_s == 6.0 and result.rollup.elapsed_s > 0


async def test_codex_delegate_effort_high_applies() -> None:
    """A real ``delegate`` to codex with ``effort="high"`` records ``effort_applied`` (F8a).

    codex encodes effort in the ACP model id, so the high tier rides a concrete base model as ``gpt-5.5[high]``
    -- an id the ``codex-acp`` adapter advertises at ``new_session`` and that the client's best-effort
    ``set_model`` then selects. A model is required for the encoding (codex's descriptor carries none by
    default), so the call names ``gpt-5.5`` explicitly. The successful turn echoes ``effort=high`` and a
    non-None ``effort_applied=high``.
    """
    registry = default_registry()
    service = DelegationService(registry, RutherfordConfig())
    request = DelegationRequest(
        target=Target(cli="codex", model="gpt-5.5"),
        prompt=_PROMPT,
        working_dir=str(Path.cwd()),
        timeout_s=180.0,
        effort=Effort.HIGH,
    )
    result = await service.delegate(request)
    assert result.ok is True, f"codex failed: {result.error}"
    assert result.effort is Effort.HIGH
    assert result.effort_applied is Effort.HIGH, f"effort_applied not set: {result.effort_applied!r}"
    assert result.target.model == "gpt-5.5[high]"  # the effort-rewritten id the agent was switched to


# --- review / plan: read-only role-driven tools against real goose -----------

#: A three-line patch with a clear, intentional bug (subtract where it should add), so a real reviewer has
#: something concrete to flag.
_REVIEW_DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a - b
"""


def _review_app() -> AppContext:
    """An AppContext over the real registry, for driving the review / plan tools end to end."""
    config = RutherfordConfig()
    return build_app_context(config=config, descriptors=default_registry())


async def test_goose_review_over_a_diff_live() -> None:
    """A real ``review`` across two goose voices over a tiny diff returns a read-only consensus (both answer).

    Drives the whole read-only review path against a real agent: the principal-reviewer persona is prepended,
    two goose voices review the patch in parallel, and the result is a ConsensusResult with both voices
    answering -- read-only, synthesized by default. The diff carries an obvious bug (subtract not add), so the
    reviewers have a real defect to find; the assertion only requires both voices to answer (a model's exact
    wording is not contractual).
    """
    out = await review_tool(
        _review_app(),
        targets=["goose", "goose"],
        diff=_REVIEW_DIFF,
        working_dir=str(Path.cwd()),
        timeout_s=120.0,
    )
    # The all-voices envelope is a quoted-array TOON the python-toon decoder cannot round-trip, so assert on the
    # encoded string: two read-only voices answered and a synthesis was produced.
    assert out.count("ok: true") >= 2, f"expected two answering goose voices, got: {out[:400]}"
    assert "safety_mode: read_only" in out  # the review ran read-only
    assert "synthesis" in out  # synthesize defaults on for review


async def test_goose_plan_live() -> None:
    """A real ``plan`` to goose for a small goal returns an ok read-only architect delegate.

    The read-only planning path against a real agent: the architect persona is prepended, goose designs an
    approach (rather than implementing it), and the result is an ok DelegationResult with safety clamped to
    read_only. Asserts only ok + read-only + non-empty text -- the plan's content is the model's, not a fixture.
    """
    out = await plan_tool(
        _review_app(),
        cli="goose",
        goal="Add a small in-memory LRU cache to a function that recomputes an expensive value.",
        working_dir=str(Path.cwd()),
        timeout_s=120.0,
    )
    result = DelegationResult.model_validate(decode(out))
    assert result.ok is True, f"plan failed: {result.error}"
    assert result.safety_mode is SafetyMode.READ_ONLY  # planning is clamped to read-only
    assert result.text.strip(), "the plan produced no text"


# --- write/propose sandbox: a real goose mutating a fresh temp git repo ------


def _git(path: Path, *args: str) -> str:
    """Run a git command in ``path`` (a sync helper, so async tests do not trip the blocking-call lint)."""
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout


def _temp_git_repo(path: Path) -> None:
    """Initialise a temp git repo with one commit, the trusted workspace a real write delegation runs in."""
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Rutherford Test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")


async def test_goose_write_mode_creates_a_file_in_the_real_repo(tmp_path: Path) -> None:
    """A real WRITE-mode delegate to goose in a fresh temp git repo creates hello.txt and applies it back.

    The live proof that an agent can do real write work safely over ACP: goose runs in an isolated worktree,
    its edit is computed as a diff and applied back to the real repo, ``changed_files`` lists the file, and the
    user's working_dir ends up holding exactly the content asked for. ``trust_workspace=True`` opts the temp
    repo past the trusted-workspace gate.
    """
    _temp_git_repo(tmp_path)
    service = DelegationService(default_registry(), RutherfordConfig())
    request = DelegationRequest(
        target=Target(cli="goose"),
        prompt="Create a file named hello.txt containing exactly this text and nothing else: hello world",
        working_dir=str(tmp_path),
        safety_mode=SafetyMode.WRITE,
        trust_workspace=True,
        timeout_s=180.0,
    )
    result = await service.delegate(request)
    assert result.ok is True, f"goose write failed: {result.error}"
    landed = tmp_path / "hello.txt"
    assert landed.is_file(), f"hello.txt did not land in the real repo; changed_files={result.changed_files}"
    assert "hello world" in landed.read_text(encoding="utf-8")
    assert result.changed_files is not None and "hello.txt" in result.changed_files
    assert result.changes_applied is True


async def test_goose_propose_mode_leaves_the_real_repo_unchanged(tmp_path: Path) -> None:
    """A real PROPOSE-mode delegate to goose returns a patch / changed_files but never touches the real repo.

    The agent edits a throwaway worktree; Rutherford captures the diff and discards the worktree, so the user's
    working_dir is byte-for-byte unchanged. Asserts the proposed file is NOT on disk and the git tree is clean,
    while the result still carries the proposed change.
    """
    _temp_git_repo(tmp_path)
    service = DelegationService(default_registry(), RutherfordConfig())
    request = DelegationRequest(
        target=Target(cli="goose"),
        prompt="Create a file named proposal.txt containing exactly this text and nothing else: a proposal",
        working_dir=str(tmp_path),
        safety_mode=SafetyMode.PROPOSE,
        trust_workspace=True,
        timeout_s=180.0,
    )
    result = await service.delegate(request)
    assert result.ok is True, f"goose propose failed: {result.error}"
    # The real repo is untouched: the proposed file is not on disk and the tree is clean.
    assert not (tmp_path / "proposal.txt").exists(), "propose mode wrote to the real repo"
    assert _git(tmp_path, "status", "--porcelain").strip() == "", "propose left the real tree dirty"
    assert result.changes_applied is False  # nothing applied
    # The proposal is still captured (changed_files and/or a diff), so the work was not lost.
    assert (result.changed_files and "proposal.txt" in result.changed_files) or (result.diff and result.diff.strip())
