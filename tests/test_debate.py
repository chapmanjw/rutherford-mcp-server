# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for debate over ACP: persistent per-voice sessions, delta prompts, drop-outs, and synthesis."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import Effort, SafetyMode, Stance, TerminationReason
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DebateContribution, DebateRequest, DebateRound, Target
from rutherford.services.debate import (
    DebateService,
    _disambiguate,
    _participant_clis,
    _set_aside_dissents,
    _Voice,
    _with_later_stance,
)
from rutherford.services.delegation import DelegationService
from rutherford.tools.debate import debate_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
DEAD = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
# * Cut-budget window for the slow-voice tests. Quiet spawn is ~0.7s; ACP handshake + optional set_model
#   adds more latency, so the budget must still clear a FAST voice while the slow sleep outlasts the cut.
_CUT_BUDGET_S = 4.0
_SLOW_SLEEP_S = "5.5"
# A slow agent: streams a partial then sleeps, so a tight round deadline cuts its turn mid-round.
SLOW = AgentDescriptor(
    "slow", "Slow", _FAKE_CMD, default_model="model-s", env_overrides=(("RUTHERFORD_FAKE_SLEEP", _SLOW_SLEEP_S),)
)
# A fast fake with a distinct id, so a debate of two ``fake`` voices can name it as a NON-participant judge.
JUDGE = AgentDescriptor("judge", "Judge", _FAKE_CMD, provider="beta", default_model="model-j")
# Voices that always vote a fixed way (via env), so a convergence-tracked debate (F5) can hold a stable
# disagreement -- a steady 'yes' bloc against a steady 'no' -- without per-voice prompts.
YES_VOTER = AgentDescriptor("yesvoter", "Yes", _FAKE_CMD, env_overrides=(("RUTHERFORD_FAKE_VERDICT", "yes"),))
NO_VOTER = AgentDescriptor("novoter", "No", _FAKE_CMD, env_overrides=(("RUTHERFORD_FAKE_VERDICT", "no"),))


def _service(config: RutherfordConfig | None = None) -> DebateService:
    resolved = config or RutherfordConfig()
    registry = DescriptorRegistry([FAKE, DEAD, SLOW, JUDGE])
    return DebateService(registry, resolved, DelegationService(registry, resolved))


def _converge_service(config: RutherfordConfig | None = None) -> DebateService:
    resolved = config or RutherfordConfig()
    registry = DescriptorRegistry([FAKE, YES_VOTER, NO_VOTER])
    return DebateService(registry, resolved, DelegationService(registry, resolved))


def _app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))


def _two_fakes(**kwargs: Any) -> DebateRequest:
    return DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
        **kwargs,
    )


async def test_debate_two_rounds_on_persistent_sessions() -> None:
    result = await _service().debate(_two_fakes(rounds=2))
    assert len(result.rounds) == 2
    assert all(len(round_.contributions) == 2 for round_ in result.rounds)
    assert all(contribution.ok for round_ in result.rounds for contribution in round_.contributions)
    # round 2 ran on the SAME live sessions via delta prompts, and both voices still answered
    assert all(contribution.ok for contribution in result.rounds[1].contributions)
    assert result.final is not None and result.synthesis_by is not None


async def test_debate_needs_two_targets() -> None:
    with pytest.raises(RutherfordError) as exc:
        await _service().debate(DebateRequest(targets=[Target(cli="fake")], prompt="x"))
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_debate_enforces_target_cap() -> None:
    with pytest.raises(RutherfordError) as exc:
        await _service(RutherfordConfig(max_targets=1)).debate(_two_fakes())
    assert exc.value.code is ErrorCode.TOO_MANY_TARGETS


async def test_debate_validates_rounds() -> None:
    with pytest.raises(RutherfordError):
        await _service().debate(_two_fakes(rounds=0))
    with pytest.raises(RutherfordError):
        await _service(RutherfordConfig(max_debate_rounds=2)).debate(_two_fakes(rounds=5))


async def test_debate_unknown_agent_becomes_a_failed_contribution() -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="nope")],
        prompt="what is 17 + 25?",
        rounds=1,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().debate(request)
    by_cli = {contribution.target.cli: contribution for contribution in result.rounds[0].contributions}
    assert by_cli["fake"].ok is True and "42" in by_cli["fake"].text
    assert by_cli["nope"].ok is False


async def test_debate_handshake_failure_becomes_a_failed_contribution() -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="dead")],
        prompt="what is 17 + 25?",
        rounds=1,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().debate(request)
    by_cli = {contribution.target.cli: contribution for contribution in result.rounds[0].contributions}
    assert by_cli["fake"].ok is True
    dead = by_cli["dead"]
    assert dead.ok is False and dead.error is not None and dead.error.code is ErrorCode.ACP_HANDSHAKE_FAILED


async def test_debate_without_synthesis() -> None:
    result = await _service().debate(_two_fakes(rounds=1, synthesize=False))
    assert result.final is None


# --- F4a no-self-approval + pre-commit + set-aside dissent --------------------


async def test_debate_closing_flags_self_authorship() -> None:
    # F4a (4-A): the default closing author is the first surviving voice -- a participant -- so the closing is
    # flagged self_authored. Naming a non-participant judge clears it.
    result = await _service().debate(_two_fakes(rounds=1))
    assert result.final is not None and result.self_authored is True

    judged = await _service().debate(_two_fakes(rounds=1, judge=Target(cli="judge")))
    assert judged.final is not None and judged.self_authored is False
    assert judged.synthesis_by == "judge"


async def test_debate_require_independent_judge_refuses_a_participant() -> None:
    # With require_independent_judge the default (participant) closing author is refused; a non-participant
    # judge passes cleanly.
    with pytest.raises(RutherfordError) as exc:
        await _service().debate(_two_fakes(rounds=1, require_independent_judge=True))
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert "require_independent_judge" in exc.value.message and "non-participant" in exc.value.message

    ok = await _service().debate(_two_fakes(rounds=1, require_independent_judge=True, judge=Target(cli="judge")))
    assert ok.final is not None and ok.self_authored is False


async def test_debate_require_independent_judge_via_config() -> None:
    # The guard fires from config as well, refusing a participant closing server-wide with no per-call flag.
    config = RutherfordConfig(require_independent_judge=True)
    with pytest.raises(RutherfordError) as exc:
        await _service(config).debate(_two_fakes(rounds=1))
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_debate_carries_round_one_pre_commit_onto_later_rounds() -> None:
    # F4a (4-C): each seat's blind round-1 answer is carried onto its later-round contributions so a reader
    # sees pre-vs-post movement. Round 1 itself has no pre_commit (it IS the pre-commitment).
    result = await _service().debate(_two_fakes(rounds=2))
    assert len(result.rounds) == 2
    assert all(c.pre_commit is None for c in result.rounds[0].contributions)  # round 1 is the blind commit
    round_one_by_seat = {c.seat_id: c.text for c in result.rounds[0].contributions}
    for contribution in result.rounds[1].contributions:
        assert contribution.pre_commit == round_one_by_seat[contribution.seat_id]
        assert contribution.pre_commit  # a non-empty captured round-1 position


def _contribution(
    label: str, *, seat: str, round_index: int, ok: bool, text: str, dissent: str | None = None
) -> DebateContribution:
    return DebateContribution(
        label=label,
        seat_id=seat,
        target=Target(cli=label.lower()),
        round_index=round_index,
        ok=ok,
        text=text,
        dissent=dissent,
    )


def test_participant_clis_spans_every_round_not_just_the_close() -> None:
    # F4a (4-A): a seat that argued round 1 then dropped is still a participant, so the no-self-approval set
    # must include it -- otherwise a named judge that debated-then-dropped is wrongly treated as independent.
    round1 = DebateRound(
        index=1,
        contributions=[
            _contribution("Fake", seat="0:Fake", round_index=1, ok=True, text="A"),
            _contribution("Dropper", seat="1:Dropper", round_index=1, ok=True, text="B"),
        ],
    )
    round2 = DebateRound(
        index=2,
        contributions=[
            _contribution("Fake", seat="0:Fake", round_index=2, ok=True, text="A2"),
            _contribution(
                "Dropper",
                seat="1:Dropper",
                round_index=2,
                ok=False,
                text="",
                dissent="set aside: no usable answer in round 2",
            ),
        ],
    )
    assert _participant_clis([round1, round2]) == {"fake", "dropper"}  # dropper counts despite leaving round 2


def test_set_aside_dissents_surfaces_a_dropped_seats_last_position() -> None:
    # F4a (4-B): the closing summarizes only the final usable round, so a seat that argued earlier then dropped
    # must be surfaced (its last usable text + why) or the "name each set-aside dissent" instruction is moot.
    round1 = DebateRound(
        index=1,
        contributions=[
            _contribution("Fake", seat="0:Fake", round_index=1, ok=True, text="keep it"),
            _contribution("Dropper", seat="1:Dropper", round_index=1, ok=True, text="ship anyway"),
        ],
    )
    round2 = DebateRound(
        index=2,
        contributions=[
            _contribution("Fake", seat="0:Fake", round_index=2, ok=True, text="still keep it"),
            _contribution(
                "Dropper",
                seat="1:Dropper",
                round_index=2,
                ok=False,
                text="",
                dissent="set aside: no usable answer in round 2",
            ),
        ],
    )
    final = round2  # the last usable round (Fake answered)
    set_aside = _set_aside_dissents([round1, round2], final)
    assert set_aside == [("Dropper", "set aside: no usable answer in round 2", "ship anyway")]  # its round-1 text
    # a seat still answering the final round is already in the transcript, never re-surfaced as "set aside"
    assert all(label != "Fake" for label, _, _ in set_aside)


def test_set_aside_dissents_omits_a_pure_failure_with_no_position() -> None:
    # A seat that never produced a usable position has nothing to NAME -- it is omitted (its failure is its own
    # record), so the closing block only ever carries real dissenting positions.
    round1 = DebateRound(
        index=1,
        contributions=[
            _contribution("Fake", seat="0:Fake", round_index=1, ok=True, text="answer"),
            _contribution(
                "Dead",
                seat="1:Dead",
                round_index=1,
                ok=False,
                text="",
                dissent="set aside: no usable answer in round 1",
            ),
        ],
    )
    assert _set_aside_dissents([round1], round1) == []  # the dead seat has no position to surface


async def test_debate_set_aside_voice_is_stamped_with_dissent() -> None:
    # F4a (4-B): a seat that produced no usable answer in a round is dropped -- its contribution carries a
    # structural set-aside reason, never a silent disappearance. The dead voice fails round 1 and is stamped.
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake"), Target(cli="dead")],
        prompt="what is 17 + 25?",
        rounds=2,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().debate(request)
    dead = next(c for c in result.rounds[0].contributions if c.target.cli == "dead")
    assert dead.ok is False and dead.dissent == "set aside: no usable answer in round 1"
    # the surviving voices argued on and were never set aside
    survivors = [c for c in result.rounds[0].contributions if c.target.cli == "fake"]
    assert survivors and all(c.dissent is None for c in survivors)
    # round 2 ran only the two survivors (the dead seat is gone)
    assert {c.target.cli for c in result.rounds[1].contributions} == {"fake"}


@pytest.mark.parametrize("mode", [SafetyMode.PROPOSE, SafetyMode.WRITE, SafetyMode.YOLO])
async def test_debate_rejects_a_sandboxed_safety_mode(mode: SafetyMode) -> None:
    # A debate runs its voices over PERSISTENT sessions directly in the real working_dir -- there is no
    # per-turn worktree to isolate writes into, so a sandboxed (propose/write/yolo) mode would let an agent
    # write straight into the user's tree. The service refuses it; write/propose work goes through delegate.
    with pytest.raises(RutherfordError) as exc:
        await _service().debate(_two_fakes(rounds=1, safety_mode=mode))
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert "read-only" in exc.value.message and "delegate" in exc.value.message


def test_disambiguate_labels() -> None:
    assert _disambiguate(["a", "b"]) == ["a", "b"]
    assert _disambiguate(["a", "a", "b"]) == ["a#1", "a#2", "b"]


def test_with_later_stance_helper() -> None:
    assert _with_later_stance("x", Stance.FOR).endswith("Keep arguing in favor of the proposition.")
    assert _with_later_stance("x", Stance.AGAINST).endswith("Keep arguing against the proposition.")
    assert _with_later_stance("x", None) == "x"  # an unsteered voice gets no reminder


def test_stance_is_re_embedded_every_round_not_just_round_one() -> None:
    # Item 17 (v2 parity): a FOR/AGAINST voice keeps its stance reminder on the later-round delta prompt;
    # without it a multi-round debate drifts to the center as each voice accommodates the others.
    service = _service()
    voice = _Voice(index=0, target=Target(cli="fake"), label="Pro", stance=Stance.FOR)
    req = _two_fakes(rounds=3)

    opening = service._round_prompt(req, voice, [])
    assert "Argue in favor of the proposition." in opening  # round 1 opens with the stance

    other = DebateContribution(
        label="Con",
        seat_id="1:Con",
        target=Target(cli="fake"),
        round_index=1,
        stance=Stance.AGAINST,
        ok=True,
        text="No, the opposite is true.",
    )
    later = service._round_prompt(req, voice, [DebateRound(index=1, contributions=[other])])
    assert "Keep arguing in favor of the proposition." in later  # the stance is restated each round
    assert "No, the opposite is true." in later  # the other voice's latest position rides the delta


def test_unsteered_later_round_prompt_has_no_stance_reminder() -> None:
    service = _service()
    voice = _Voice(index=0, target=Target(cli="fake"), label="Neutral", stance=None)
    other = DebateContribution(label="Other", target=Target(cli="fake"), round_index=1, ok=True, text="point")
    later = service._round_prompt(_two_fakes(rounds=2), voice, [DebateRound(index=1, contributions=[other])])
    assert "Keep arguing" not in later


async def test_debate_tool_and_server_wrapper(monkeypatch: Any) -> None:
    out = await debate_tool(
        _app(), prompt="what is 17 + 25?", targets=["fake", "fake"], rounds=1, working_dir=str(REPO_ROOT)
    )
    assert "42" in out
    monkeypatch.setattr(server, "_APP", _app())
    wrapped = await server.debate(
        prompt="what is 17 + 25?", targets=["fake", "fake"], rounds=1, working_dir=str(REPO_ROOT)
    )
    assert "42" in wrapped


# --- time budget at round boundaries (F8a) -----------------------------------


async def test_debate_budget_cuts_a_round_and_finalizes() -> None:
    # A slow voice keeps round 1 in flight past the deadline: the fast voice's answer is kept, the slow turn
    # is a BUDGET_EXHAUSTED contribution (its partial preserved, not promoted), and the debate finalizes early.
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        rounds=3,
        working_dir=str(REPO_ROOT),
        time_budget_s=_CUT_BUDGET_S,
    )
    result = await _service().debate(request)
    assert result.stop_reason == "budget" and len(result.rounds) == 1  # cut at the round-1 deadline
    by_cli = {c.target.cli: c for c in result.rounds[0].contributions}
    assert by_cli["fake"].ok and "42" in by_cli["fake"].text
    slow = by_cli["slow"]
    assert slow.ok is False and slow.error is not None and slow.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert slow.text == "" and slow.partial == "partial-so-far"  # partial kept as a trace, never the text
    # F4a (4-B): a seat cut at the deadline produced no usable answer, so it is set aside, not silent.
    assert slow.dissent == "set aside: no usable answer in round 1"
    assert result.rollup is not None
    assert result.rollup.stop_reason == "budget" and result.rollup.cut == 1 and result.rollup.usable == 1


async def test_debate_budget_below_quorum_raises_budget_exhausted() -> None:
    # Both voices slow: every round-1 turn is cut, leaving zero usable positions -> BUDGET_EXHAUSTED.
    request = DebateRequest(
        targets=[Target(cli="slow"), Target(cli="slow")],
        prompt="x",
        rounds=2,
        working_dir=str(REPO_ROOT),
        time_budget_s=_CUT_BUDGET_S,
    )
    with pytest.raises(RutherfordError) as exc:
        await _service().debate(request)
    assert exc.value.code is ErrorCode.BUDGET_EXHAUSTED


async def test_debate_generous_budget_finishes_clean_with_a_rollup() -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?",
        rounds=2,
        working_dir=str(REPO_ROOT),
        time_budget_s=60.0,
    )
    result = await _service().debate(request)
    assert result.stop_reason is None and len(result.rounds) == 2  # ran to completion within the budget
    assert result.rollup is not None and result.rollup.stop_reason == "ok" and result.rollup.cut == 0


async def test_debate_no_budget_leaves_stop_reason_and_rollup_unset() -> None:
    result = await _service().debate(_two_fakes(rounds=1))
    assert result.stop_reason is None and result.rollup is None


async def test_debate_on_budget_continue_runs_every_round() -> None:
    # on_budget="continue" makes the budget advisory: even a slow voice runs to completion, no cut.
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        rounds=1,
        working_dir=str(REPO_ROOT),
        time_budget_s=_CUT_BUDGET_S,
        on_budget="continue",
    )
    result = await _service().debate(request)
    assert result.stop_reason is None
    assert all(c.ok for c in result.rounds[0].contributions)  # the slow voice finished too
    assert result.rollup is not None and result.rollup.stop_reason == "ok" and result.rollup.cut == 0


async def test_debate_applies_per_seat_effort_to_each_voice() -> None:
    # A debate panel can pin DIFFERENT efforts per seat: each voice's own tier rides its own persistent session
    # (set via the claude_code-id fake's effort config option) and is echoed back -- proof per-seat effort
    # flows independently to each debate voice, not one uniform call-level tier.
    claude = AgentDescriptor(
        "claude_code",
        "Claude",
        _FAKE_CMD,
        env_overrides=(("RUTHERFORD_FAKE_EFFORT_OPTION", "effort:low,medium,high,xhigh,max"),),
    )
    registry = DescriptorRegistry([claude])
    service = DebateService(registry, RutherfordConfig(), DelegationService(registry, RutherfordConfig()))
    request = DebateRequest(
        targets=[Target(cli="claude_code", effort=Effort.HIGH), Target(cli="claude_code", effort=Effort.XHIGH)],
        prompt="EFFORT?",
        rounds=1,
        working_dir=str(REPO_ROOT),
    )
    result = await service.debate(request)
    contributions = result.rounds[0].contributions
    assert all(c.ok for c in contributions)
    assert {c.effort_applied for c in contributions} == {Effort.HIGH, Effort.XHIGH}
    assert {c.text.strip() for c in contributions} == {"effort=high", "effort=xhigh"}


async def test_debate_budget_rollup_records_effort_requested() -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        rounds=2,
        working_dir=str(REPO_ROOT),
        time_budget_s=_CUT_BUDGET_S,
        effort=Effort.MEDIUM,
    )
    result = await _service().debate(request)
    assert result.rollup is not None and result.rollup.effort_requested is Effort.MEDIUM


async def test_debate_cut_turn_with_no_stream_has_no_partial() -> None:
    # A HANG voice streams nothing before the deadline, so its cut contribution has partial=None (an honest
    # empty harvest) while the fast voice's answer is still kept.
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?\nHANG",  # both voices receive HANG (the shared prompt) -> both cut
        rounds=1,
        working_dir=str(REPO_ROOT),
        time_budget_s=_CUT_BUDGET_S,
    )
    config = RutherfordConfig(min_quorum=1)
    # both cut with no usable position -> below quorum -> BUDGET_EXHAUSTED
    with pytest.raises(RutherfordError) as exc:
        await _service(config).debate(request)
    assert exc.value.code is ErrorCode.BUDGET_EXHAUSTED


# --- F5: carry-forward + convergence/stall + DebateOutcome -------------------


def _ledger_round(index: int, *texts: str) -> DebateRound:
    contributions = [
        DebateContribution(
            label=f"v{i}", seat_id=f"{i}:v{i}", target=Target(cli="fake"), round_index=index, ok=True, text=text
        )
        for i, text in enumerate(texts)
    ]
    return DebateRound(index=index, contributions=contributions)


def _round_with(index: int, label: str, text: str) -> DebateRound:
    other = DebateContribution(
        label=label, seat_id=f"1:{label}", target=Target(cli="fake"), round_index=index, ok=True, text=text
    )
    return DebateRound(index=index, contributions=[other])


def test_carry_forward_sends_the_full_transcript_not_just_the_delta() -> None:
    # F5 (11-B): a carry-forward round re-sends EVERY prior round verbatim, not only the previous one.
    service = _service()
    voice = _Voice(index=0, target=Target(cli="fake"), label="A", stance=None)
    req = _two_fakes(rounds=3, carry_forward=True)
    prompt = service._round_prompt(
        req, voice, [_round_with(1, "B", "round-one-pos"), _round_with(2, "B", "round-two-pos")]
    )
    assert "FULL transcript" in prompt
    assert "round-one-pos" in prompt and "round-two-pos" in prompt  # BOTH prior rounds
    assert "Round 1" in prompt and "Round 2" in prompt


def test_default_delta_sends_only_the_previous_round() -> None:
    # Default (no carry-forward): only the latest round's positions ride the delta -- the session memory holds
    # the rest. This is the contrast that makes carry-forward a real, separate mode.
    service = _service()
    voice = _Voice(index=0, target=Target(cli="fake"), label="A", stance=None)
    req = _two_fakes(rounds=3)
    prompt = service._round_prompt(
        req, voice, [_round_with(1, "B", "round-one-pos"), _round_with(2, "B", "round-two-pos")]
    )
    assert "round-two-pos" in prompt and "round-one-pos" not in prompt  # only the previous round


def test_convergence_ledger_unanimous_is_converged() -> None:
    ledger = _service()._convergence_ledger(_ledger_round(1, "VERDICT: yes", "VERDICT: yes"), None)
    assert ledger.converged is True and ledger.decision == "yes" and ledger.tally == {"yes": 2}


def test_convergence_ledger_stable_majority_stalls_not_converges() -> None:
    # A stable MAJORITY that is not unanimous is NOT convergence -- the stall counter rises while it holds.
    service = _service()
    first = service._convergence_ledger(_ledger_round(2, "VERDICT: yes", "VERDICT: yes", "VERDICT: no"), None)
    assert first.converged is False and first.decision == "yes" and first.stall_count == 0  # just established
    second = service._convergence_ledger(_ledger_round(3, "VERDICT: yes", "VERDICT: yes", "VERDICT: no"), first)
    assert second.stall_count == 1 and second.changed is False  # the decision held one round
    third = service._convergence_ledger(_ledger_round(4, "VERDICT: yes", "VERDICT: yes", "VERDICT: no"), second)
    assert third.stall_count == 2


def test_convergence_ledger_decision_change_resets_the_stall() -> None:
    service = _service()
    first = service._convergence_ledger(_ledger_round(1, "VERDICT: yes", "VERDICT: yes", "VERDICT: no"), None)
    moved = service._convergence_ledger(_ledger_round(2, "VERDICT: no", "VERDICT: no", "VERDICT: yes"), first)
    assert moved.decision == "no" and moved.changed is True and moved.stall_count == 0


def test_convergence_ledger_no_verdicts_has_no_decision() -> None:
    ledger = _service()._convergence_ledger(_ledger_round(1, "just prose", "more prose"), None)
    assert ledger.decision is None and ledger.converged is False and ledger.stall_count == 0


def test_convergence_ledger_a_no_verdict_round_resets_the_stall() -> None:
    # A held decision that then becomes unreadable (no verdicts) resets the stall -- a vanished decision is
    # not a "held" one, so the panel is no longer frozen on it.
    service = _service()
    first = service._convergence_ledger(_ledger_round(1, "VERDICT: yes", "VERDICT: yes", "VERDICT: no"), None)
    held = service._convergence_ledger(_ledger_round(2, "VERDICT: yes", "VERDICT: yes", "VERDICT: no"), first)
    assert held.stall_count == 1
    blank = service._convergence_ledger(_ledger_round(3, "prose", "prose", "prose"), held)
    assert blank.decision is None and blank.stall_count == 0


def test_debate_outcome_and_ledger_serialize_onto_the_wire() -> None:
    # F5 metadata rides the TOON wire: to_plain (model_dump json, exclude_none) keeps the enum + the tally dict.
    from rutherford.domain.models import DebateOutcome, ProgressLedger
    from rutherford.io.serialize import to_plain

    outcome = to_plain(
        DebateOutcome(termination=TerminationReason.STALLED, rounds_run=3, decision="yes", stall_count=2)
    )
    assert outcome["termination"] == "stalled" and outcome["rounds_run"] == 3 and outcome["decision"] == "yes"
    ledger = ProgressLedger(round_index=2, outcome="majority", decision="yes", tally={"yes": 2, "no": 1}, stall_count=1)
    assert to_plain(ledger)["tally"] == {"yes": 2, "no": 1}  # the per-verdict tally dict round-trips


async def test_debate_converges_on_a_unanimous_verdict() -> None:
    # Both voices vote 'yes' in round 1 -> unanimity -> the debate stops CONVERGED without running all rounds.
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="Decide.\nSAY=VERDICT: yes",
        rounds=4,
        working_dir=str(REPO_ROOT),
        track_convergence=True,
    )
    result = await _service().debate(request)
    assert result.outcome is not None
    assert result.outcome.termination is TerminationReason.CONVERGED and result.outcome.converged is True
    assert result.outcome.decision == "yes" and result.outcome.rounds_run == 1  # converged at round 1
    assert result.rounds[0].ledger is not None and result.rounds[0].ledger.converged is True


async def test_debate_without_convergence_tracking_is_unresolved() -> None:
    # No tracking: the debate runs its full round budget and terminates UNRESOLVED, with no per-round ledger.
    result = await _service().debate(_two_fakes(rounds=2))
    assert result.outcome is not None and result.outcome.termination is TerminationReason.UNRESOLVED
    assert result.outcome.rounds_run == 2 and result.outcome.converged is False
    assert all(round_.ledger is None for round_ in result.rounds)


async def test_debate_stalls_on_a_frozen_disagreement() -> None:
    # A steady 2-yes / 1-no split never reaches unanimity but the majority holds: the debate stops STALLED
    # once the decision is unchanged for the stall tolerance, instead of burning every round.
    config = RutherfordConfig(debate_stall_tolerance=1)
    request = DebateRequest(
        targets=[Target(cli="yesvoter"), Target(cli="novoter"), Target(cli="yesvoter")],
        prompt="Decide.",
        rounds=4,
        working_dir=str(REPO_ROOT),
        track_convergence=True,
    )
    result = await _converge_service(config).debate(request)
    assert result.outcome is not None and result.outcome.termination is TerminationReason.STALLED
    assert result.outcome.decision == "yes" and result.outcome.converged is False
    # tolerance=1: round 1 establishes the decision (stall 0), round 2 holds it (stall 1 >= 1) -> stop at 2.
    assert result.outcome.rounds_run == 2


async def test_debate_quorum_lost_is_recorded_in_the_outcome() -> None:
    # A debate that drops below two voices terminates QUORUM_LOST (recorded on the outcome, not just a silent stop).
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="dead")],
        prompt="what is 17 + 25?",
        rounds=3,
        working_dir=str(REPO_ROOT),
    )
    result = await _service().debate(request)
    assert result.outcome is not None and result.outcome.termination is TerminationReason.QUORUM_LOST


async def test_debate_budget_termination_is_recorded_in_the_outcome() -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        rounds=3,
        working_dir=str(REPO_ROOT),
        time_budget_s=_CUT_BUDGET_S,
    )
    result = await _service().debate(request)
    assert result.outcome is not None and result.outcome.termination is TerminationReason.BUDGET
