# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for consensus over ACP: fan-out, caps, strategies, verdicts, synthesis, diversity, expand_all."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import Effort, SafetyMode, Stance, Strategy
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ConsensusRequest, ConsensusResult, StrategyResult, Target
from rutherford.services.consensus import ConsensusService
from rutherford.services.delegation import DelegationService
from rutherford.tools.common import as_target, ensure_known_targets, parse_stances, parse_strategy
from rutherford.tools.consensus import consensus_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
# Two more fakes with distinct provider + default model, so a panel of them spans real diversity.
FAKE_A = AgentDescriptor("fake_a", "Fake A", _FAKE_CMD, provider="alpha", default_model="model-a")
FAKE_B = AgentDescriptor("fake_b", "Fake B", _FAKE_CMD, provider="beta", default_model="model-b")
# An agent that exits before the handshake, so its voice always fails.
DEAD = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
# A slow agent: it streams a partial then sleeps 1.5s, so a tight panel deadline cuts it mid-turn. Slowness
# rides the descriptor env, not the prompt, so a panel can mix a fast voice and a slow one on one prompt. The
# sleep is kept small on purpose: the budget/cut/harvest logic works at asyncio resolution, so the slow voice
# only needs to outlast the ~1.2s cut budget by a clear margin (it finishes near subprocess-spawn + 1.5s),
# not take whole seconds. A floor of ~0.7s of subprocess-spawn overhead per voice is why the budgets below
# sit near a second rather than truly sub-second: a budget under the spawn floor would cut the FAST voice too.
SLOW = AgentDescriptor(
    "slow",
    "Slow",
    _FAKE_CMD,
    provider="gamma",
    default_model="model-s",
    env_overrides=(("RUTHERFORD_FAKE_SLEEP", "1.5"),),
)


def _registry(extra: list[AgentDescriptor] | None = None) -> DescriptorRegistry:
    return DescriptorRegistry([FAKE, FAKE_A, FAKE_B, *(extra or [])])


def _service(config: RutherfordConfig | None = None, extra: list[AgentDescriptor] | None = None) -> ConsensusService:
    resolved = config or RutherfordConfig()
    registry = _registry(extra)
    return ConsensusService(DelegationService(registry, resolved), registry, resolved)


def _app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=_registry())


def _prompt(say: str) -> str:
    return f"Decide.\nSAY={say}"


# --- the legacy all-voices path ----------------------------------------------


async def test_consensus_collects_every_voice() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake", model="m")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert len(result.voices) == 2
    assert all(voice.ok and "42" in voice.text for voice in result.voices)


async def test_consensus_requires_a_target() -> None:
    with pytest.raises(RutherfordError) as exc:
        await _service().consensus(ConsensusRequest(targets=[], prompt="x"))
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_consensus_enforces_target_cap() -> None:
    config = RutherfordConfig(max_targets=1)
    with pytest.raises(RutherfordError) as exc:
        await _service(config).consensus(ConsensusRequest(targets=[Target(cli="fake"), Target(cli="fake")], prompt="x"))
    assert exc.value.code is ErrorCode.TOO_MANY_TARGETS


@pytest.mark.parametrize("mode", [SafetyMode.PROPOSE, SafetyMode.WRITE, SafetyMode.YOLO])
async def test_consensus_rejects_a_sandboxed_safety_mode(mode: SafetyMode) -> None:
    # A consensus asks many agents one question; there is no coherent merge of edits from several of them into
    # one tree, and the budgeted-harvest path drives sessions directly in the real working_dir with no per-turn
    # sandbox. So a sandboxed (propose/write/yolo) mode is refused in the service; writes go through delegate.
    with pytest.raises(RutherfordError) as exc:
        await _service().consensus(
            ConsensusRequest(targets=[Target(cli="fake")], prompt="x", safety_mode=mode, working_dir=str(REPO_ROOT))
        )
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert "read-only" in exc.value.message and "delegate" in exc.value.message


async def test_consensus_diversity_is_low_for_same_model() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake", model="m"), Target(cli="fake", model="m")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.diversity is not None and result.diversity.low_diversity is True


async def test_consensus_diversity_high_across_distinct_models() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake_a"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.diversity is not None and result.diversity.low_diversity is False
    assert result.diversity.distinct_models == 2 and result.diversity.distinct_providers == 2


# --- synthesis ---------------------------------------------------------------


async def test_consensus_synthesize_picks_a_synthesizer() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?",
        synthesize=True,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.synthesis is not None and result.synthesis_by is not None


async def test_consensus_synthesize_off_by_default() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")], prompt="what is 17 + 25?", working_dir=str(REPO_ROOT)
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.synthesis is None and result.synthesis_by is None


async def test_consensus_synthesize_uses_named_judge() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake_a"), Target(cli="fake_a")],
        prompt="what is 17 + 25?",
        synthesize=True,
        judge=Target(cli="fake_b"),
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.synthesis_by == "fake_b"


# --- strategies & verdict extraction -----------------------------------------


async def test_strategy_unanimous_from_verdict_lines() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt=_prompt("VERDICT: yes"),
        strategy=Strategy.UNANIMOUS,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.outcome == "unanimous" and result.decision == "yes"
    assert all(voice.verdict == "yes" for voice in result.voices)


async def test_strategy_majority_true_majority() -> None:
    # the fake echoes one shared prompt, so every voice plants the same verdict; three yeses are a
    # true majority of three eligible voices (the dissent path is covered in test_strategies.py).
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake"), Target(cli="fake")],
        prompt=_prompt("VERDICT: yes"),
        strategy=Strategy.MAJORITY,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.outcome == "majority" and result.decision == "yes"


async def test_strategy_verdict_via_json_schema() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt=_prompt('{"verdict": "approve"}'),
        strategy=Strategy.UNANIMOUS,
        verdict_schema={"verdict": "string"},
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.decision == "approve"


async def test_strategy_unparseable_voice_is_recorded_not_dropped() -> None:
    # a prose answer with no VERDICT line (and a verdict_schema expecting JSON it never emits) -> every
    # voice is unparseable, recorded with a reason rather than silently dropped; 0 parseable < the
    # default min_quorum of 1 -> no_quorum.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="Decide yes or no, but answer in prose only.",
        strategy=Strategy.UNANIMOUS,
        verdict_schema={"verdict": "string"},
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, StrategyResult)
    assert all(voice.verdict is None and voice.no_verdict_reason == "unparseable" for voice in result.voices)
    assert result.outcome == "no_quorum"


async def test_strategy_no_quorum_when_below_min_quorum() -> None:
    config = RutherfordConfig(min_quorum=2)
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="Decide yes or no.",  # both unparseable -> 0 parseable < min_quorum
        strategy=Strategy.MAJORITY,
        working_dir=str(REPO_ROOT),
    )
    result = await _service(config).consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.outcome == "no_quorum" and result.decision is None


async def test_strategy_weighted_and_parity_metadata_flow_through() -> None:
    # the proposer (heavy) and a parity counterweight both say ship -> agree
    request = ConsensusRequest(
        targets=[
            Target(cli="fake", weight=3.0, label="proposer"),
            Target(cli="fake", parity=True),
        ],
        prompt=_prompt("VERDICT: ship"),
        strategy=Strategy.PARITY_PAIR,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.outcome == "agree" and result.decision == "ship"
    proposer = next(v for v in result.voices if v.label == "proposer")
    assert proposer.weight == 3.0
    assert any(v.parity for v in result.voices)


# --- failed-voice edges ------------------------------------------------------


async def test_failed_voice_recorded_in_strategy() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="dead")],
        prompt=_prompt("VERDICT: yes"),
        strategy=Strategy.UNANIMOUS,
        working_dir=str(REPO_ROOT),
    )
    result = await _service(extra=[DEAD]).consensus(request)
    assert isinstance(result, StrategyResult)
    dead = next(v for v in result.voices if v.cli == "dead")
    assert dead.ok is False and dead.no_verdict_reason == "failed" and dead.verdict is None
    # one failed voice vetoes unanimity (it stays in the denominator)
    assert result.outcome == "split"


async def test_diversity_none_when_no_voice_answers() -> None:
    request = ConsensusRequest(targets=[Target(cli="dead"), Target(cli="dead")], prompt="x", working_dir=str(REPO_ROOT))
    result = await _service(extra=[DEAD]).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert all(not voice.ok for voice in result.voices)
    assert result.diversity is None  # nothing answered, nothing to measure


async def test_synthesize_returns_nothing_when_all_voices_fail() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="dead"), Target(cli="dead")],
        prompt="x",
        synthesize=True,
        working_dir=str(REPO_ROOT),
    )
    result = await _service(extra=[DEAD]).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.synthesis is None and result.synthesis_by is None


async def test_synthesize_returns_nothing_when_judge_fails() -> None:
    # a named judge that cannot run -> no synthesis is produced, so synthesis_by names no one
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?",
        synthesize=True,
        judge=Target(cli="dead"),
        working_dir=str(REPO_ROOT),
    )
    result = await _service(extra=[DEAD]).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.synthesis is None and result.synthesis_by is None


# --- stances -----------------------------------------------------------------


async def test_consensus_stances_length_must_match() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="x",
        stances=[Stance.FOR],
        working_dir=str(REPO_ROOT),
    )
    with pytest.raises(RutherfordError) as exc:
        await _service().consensus(request)
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_per_seat_stance_steers_the_prompt() -> None:
    # a per-seat stance is echoed into the prompt the voice receives; the fake echoes it back, so the
    # stance wrapper ("Argue in favor") shows up in that voice's answer text.
    request = ConsensusRequest(
        targets=[Target(cli="fake", stance=Stance.FOR)],
        prompt="ship it?",
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert "Argue in favor" in result.voices[0].text


# --- expand_all --------------------------------------------------------------


async def test_expand_all_fans_to_every_registered_agent() -> None:
    request = ConsensusRequest(prompt="what is 17 + 25?", expand_all=True, working_dir=str(REPO_ROOT))
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert {voice.target.cli for voice in result.voices} == {"fake", "fake_a", "fake_b"}
    assert result.skipped == []


async def test_expand_all_records_skipped_over_cap() -> None:
    config = RutherfordConfig(max_targets=2)
    request = ConsensusRequest(prompt="what is 17 + 25?", expand_all=True, working_dir=str(REPO_ROOT))
    result = await _service(config).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert len(result.voices) == 2
    assert len(result.skipped) == 1 and "max_targets" in result.skipped[0].reason


async def test_expand_all_rejects_stances() -> None:
    request = ConsensusRequest(prompt="x", expand_all=True, stances=[Stance.FOR], working_dir=str(REPO_ROOT))
    with pytest.raises(RutherfordError) as exc:
        await _service().consensus(request)
    assert exc.value.code is ErrorCode.INVALID_INPUT


# --- tool / server wiring ----------------------------------------------------


def test_as_target_and_known_targets() -> None:
    assert as_target("fake").cli == "fake"
    assert as_target("fake:m").model == "m"
    assert as_target({"cli": "fake", "model": "m"}).model == "m"
    assert as_target({"cli": "fake", "weight": 2.0, "parity": True, "stance": "for"}).weight == 2.0
    assert as_target(Target(cli="fake")).cli == "fake"
    for bad in ({"model": "m"}, ":nope", 123, {"cli": "fake", "weight": -1.0}, {"cli": "fake", "stance": "sideways"}):
        with pytest.raises(RutherfordError):
            as_target(bad)  # type: ignore[arg-type]
    registry = _registry()
    ensure_known_targets(registry, [Target(cli="fake")])
    with pytest.raises(RutherfordError):
        ensure_known_targets(registry, [Target(cli="nope")])


def test_parse_strategy_and_stances() -> None:
    assert parse_strategy("majority") is Strategy.MAJORITY
    assert parse_stances(["for", "against"]) == [Stance.FOR, Stance.AGAINST]
    assert parse_stances(None) is None
    with pytest.raises(RutherfordError):
        parse_strategy("nope")
    with pytest.raises(RutherfordError):
        parse_stances(["sideways"])


async def test_consensus_tool_all_voices_and_server_wrapper(monkeypatch: Any) -> None:
    out = await consensus_tool(
        _app(), prompt="what is 17 + 25?", targets=["fake", "fake:m"], working_dir=str(REPO_ROOT)
    )
    assert out.count('text: "42"') == 2
    monkeypatch.setattr(server, "_APP", _app())
    wrapped = await server.consensus(prompt="what is 17 + 25?", targets=["fake"], working_dir=str(REPO_ROOT))
    assert "42" in wrapped


async def test_consensus_tool_strategy_outcome() -> None:
    out = await consensus_tool(
        _app(),
        prompt=_prompt("VERDICT: yes"),
        targets=["fake", "fake"],
        strategy="unanimous",
        working_dir=str(REPO_ROOT),
    )
    assert "unanimous" in out and "yes" in out


async def test_consensus_tool_expand_all_via_all_sentinel() -> None:
    out = await consensus_tool(_app(), prompt="what is 17 + 25?", targets="all", working_dir=str(REPO_ROOT))
    assert out.count('text: "42"') == 3  # fanned out to all three registered fakes


async def test_consensus_tool_async_runs_same_aggregating_path(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", _app())
    submit = await server.consensus(
        prompt=_prompt("VERDICT: yes"),
        targets=["fake", "fake"],
        strategy="majority",
        mode="async",
    )
    assert "job_id" in submit


# --- time budget + harvest (F8a) ---------------------------------------------


async def test_budget_cuts_the_inflight_voice_and_keeps_the_fast_one() -> None:
    # A budget shorter than the slow voice cuts it (harvesting its streamed partial) while the fast voice
    # answers; the panel succeeds with stop_reason="budget" and a rollup recording the cut.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
    )
    result = await _service(extra=[SLOW]).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.stop_reason == "budget"
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["fake"].ok and "42" in by_cli["fake"].text and by_cli["fake"].stop_reason is None
    slow = by_cli["slow"]
    assert slow.stop_reason == "budget" and slow.text == "partial-so-far"  # the streamed partial was harvested
    assert result.rollup is not None
    assert result.rollup.stop_reason == "budget" and result.rollup.cut == 1 and result.rollup.answered == 1
    assert result.rollup.usable == 2 and result.rollup.quorum_met is True
    assert result.rollup.time_budget_s == 1.2 and result.rollup.elapsed_s > 0


async def test_budget_below_quorum_raises_budget_exhausted() -> None:
    # Two slow voices, each cut with only a streamed partial (usable), but min_quorum=3 cannot be met -> the
    # genuine starved harvest raises BUDGET_EXHAUSTED rather than certifying off too few.
    config = RutherfordConfig(min_quorum=3)
    request = ConsensusRequest(
        targets=[Target(cli="slow"), Target(cli="slow")],
        prompt="x",
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
    )
    with pytest.raises(RutherfordError) as exc:
        await _service(config, extra=[SLOW]).consensus(request)
    assert exc.value.code is ErrorCode.BUDGET_EXHAUSTED


async def test_generous_budget_finishes_clean_with_a_rollup() -> None:
    # A budget longer than every voice: nothing is cut, the result-level stop_reason stays None (a clean
    # finish), and the rollup records stop_reason="ok" with the real counts.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        time_budget_s=30.0,
    )
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.stop_reason is None
    assert result.rollup is not None and result.rollup.stop_reason == "ok"
    assert result.rollup.cut == 0 and result.rollup.answered == 2 and result.rollup.usable == 2


async def test_no_budget_leaves_stop_reason_and_rollup_unset() -> None:
    request = ConsensusRequest(targets=[Target(cli="fake")], prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    result = await _service().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.stop_reason is None and result.rollup is None


async def test_on_budget_continue_runs_every_voice_to_completion() -> None:
    # With on_budget="continue" the budget is advisory: even a voice slower than the budget runs to its full
    # answer (no cut), so stop_reason stays None and nothing is harvested.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
        on_budget="continue",
    )
    result = await _service(extra=[SLOW]).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.stop_reason is None
    assert all(voice.ok and "42" in voice.text for voice in result.voices)  # the slow voice finished too
    assert result.rollup is not None and result.rollup.stop_reason == "ok" and result.rollup.cut == 0


async def test_budget_carries_into_a_strategy_result() -> None:
    # The budget path composes with the aggregation: a strategy run under a tight budget still cuts the slow
    # voice and stamps stop_reason + rollup on the StrategyResult.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt=_prompt("VERDICT: yes"),
        strategy=Strategy.PLURALITY,
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
    )
    result = await _service(extra=[SLOW]).consensus(request)
    assert isinstance(result, StrategyResult)
    assert result.stop_reason == "budget" and result.rollup is not None and result.rollup.cut == 1


async def test_budget_rollup_records_effort_requested() -> None:
    # The rollup surfaces the effort tier asked of the voices, even for fakes whose applied tier is a no-op.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
        effort=Effort.HIGH,
    )
    result = await _service(extra=[SLOW]).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.rollup is not None and result.rollup.effort_requested is Effort.HIGH


async def test_budget_path_handles_a_failed_voice() -> None:
    # The budgeted path still records a handshake-failing voice as a structured failed voice (not a cut),
    # alongside the fast voice that answered -- one bad voice never aborts a budgeted panel.
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="dead")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        time_budget_s=10.0,
    )
    result = await _service(extra=[DEAD]).consensus(request)
    assert isinstance(result, ConsensusResult)
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["fake"].ok and "42" in by_cli["fake"].text
    assert by_cli["dead"].ok is False  # handshake failure, surfaced as a failed voice, not a cut
    assert result.stop_reason is None  # the dead voice finished (failed) before the deadline -- no cut
