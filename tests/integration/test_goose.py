# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration tests: drive the real ``goose acp`` agent over ACP (local only, run with -m integration).

These verify the full ACP-native stack -- delegate, consensus, and debate (persistent sessions) -- against
a real agent, not the fake one. Slow (real model calls); deselected by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry, default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import Effort, SafetyMode, Strategy
from rutherford.domain.models import (
    ConsensusRequest,
    ConsensusResult,
    DebateRequest,
    DelegationRequest,
    StrategyResult,
    Target,
)
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService

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
