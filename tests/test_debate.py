# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the debate service, driven by fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import Stance
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DebateRequest, InvocationSpec, ProcessResult, Target
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


def _debate(
    adapters: list[FakeAdapter],
    runner: FakeProcessRunner,
    config: RutherfordConfig | None = None,
) -> DebateService:
    cfg = config or RutherfordConfig()
    registry = AdapterRegistry(adapters)
    delegation = DelegationService(registry, runner, cfg, load_roles())
    return DebateService(delegation, cfg)


def _prompts(runner: FakeProcessRunner) -> list[str]:
    """The prompt each delegation was given (FakeAdapter argv is ``[id, "-p", prompt]``)."""
    return [spec.argv[2] for spec, _ in runner.calls]


async def test_single_round_returns_one_round_per_voice() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="my answer"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="best db?", rounds=1)
    )
    assert len(result.rounds) == 1
    contributions = result.rounds[0].contributions
    assert {c.label for c in contributions} == {"a", "b"}
    assert all(c.ok for c in contributions)
    assert all(c.round_index == 1 for c in contributions)
    assert result.final == "my answer"  # synthesize is on by default


async def test_later_rounds_show_each_voice_the_others() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    await service.debate(DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="rewrite in Rust?", rounds=2))
    prompts = _prompts(runner)
    # Round one is the bare question; round two asks each voice to rebut the others.
    assert any("rewrite in Rust?" in prompt and "Critique" not in prompt for prompt in prompts)
    assert any("Critique the other positions" in prompt for prompt in prompts)
    assert any("other participants' latest positions" in prompt for prompt in prompts)


async def test_a_raising_seat_becomes_a_failed_contribution_not_a_round_abort() -> None:
    # The debate shares consensus's partial-failure contract: a seat whose adapter probe RAISES
    # must be recorded as a failed contribution while the other seat's turn survives and the
    # debate runs to completion (later rounds proceed with the survivors).
    class _DetectRaises(FakeAdapter):
        def detect(self):
            raise RuntimeError("probe exploded")

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b"), _DetectRaises("boom")], runner)
    result = await service.debate(
        DebateRequest(
            targets=[Target(cli="a"), Target(cli="b"), Target(cli="boom")],
            prompt="q",
            rounds=2,
            synthesize=False,
        )
    )
    round_one = {c.label: c for c in result.rounds[0].contributions}
    assert round_one["a"].ok and round_one["b"].ok
    assert not round_one["boom"].ok
    assert round_one["boom"].error is not None
    assert round_one["boom"].error.code == "INTERNAL"
    assert len(result.rounds) == 2  # the debate completed with the two surviving seats
    assert {c.label for c in result.rounds[1].contributions} == {"a", "b"}  # the raiser dropped out
    assert all(c.ok for c in result.rounds[1].contributions)


async def test_a_cancellation_escaping_a_seat_propagates_not_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mirrors the consensus pin: a CancelledError captured by the round's return_exceptions gather
    # must re-raise, not fold into a failed contribution -- a cancelled debate must not "complete"
    # with a swallowed cancellation.
    import asyncio

    from rutherford.domain.models import DelegationRequest

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    real_delegate = DelegationService.delegate

    async def cancelled(self: DelegationService, req: DelegationRequest, **kwargs: object):
        if req.target.cli == "b":
            raise asyncio.CancelledError()
        return await real_delegate(self, req, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DelegationService, "delegate", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.debate(
            DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=1, synthesize=False)
        )


async def test_an_exception_escaping_a_seat_delegation_is_folded_into_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense in depth behind delegate()'s containment: an exception that still escapes one
    # seat's delegation becomes that seat's failed contribution, not a lost round.
    from rutherford.domain.models import DelegationRequest

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    real_delegate = DelegationService.delegate

    async def explode(self: DelegationService, req: DelegationRequest, **kwargs: object):
        if req.target.cli == "b":
            raise RuntimeError("escaped containment")
        return await real_delegate(self, req, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DelegationService, "delegate", explode)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=1, synthesize=False)
    )
    contributions = {c.label: c for c in result.rounds[0].contributions}
    assert contributions["a"].ok
    assert not contributions["b"].ok
    assert contributions["b"].error is not None
    assert contributions["b"].error.code == "INTERNAL"


async def test_failed_voice_drops_out_of_later_rounds() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    # "b" is not installed: it fails round one and should not appear in round two.
    service = _debate([FakeAdapter("a"), FakeAdapter("b", installed=False), FakeAdapter("c")], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b"), Target(cli="c")], prompt="q", rounds=2)
    )
    round_one = {c.label: c for c in result.rounds[0].contributions}
    assert set(round_one) == {"a", "b", "c"}
    assert not round_one["b"].ok
    assert round_one["b"].error is not None and round_one["b"].error.code == "BINARY_NOT_FOUND"
    round_two = {c.label for c in result.rounds[1].contributions}
    assert round_two == {"a", "c"}  # the failed voice fell out


async def test_debate_stops_when_fewer_than_two_voices_remain() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    # With only "a" surviving round one, there is no one left to debate, so round two is skipped.
    service = _debate([FakeAdapter("a"), FakeAdapter("b", installed=False)], runner)
    result = await service.debate(DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=3))
    assert len(result.rounds) == 1


async def test_stances_steer_round_one_and_persist() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    await service.debate(
        DebateRequest(
            targets=[Target(cli="a"), Target(cli="b")],
            prompt="adopt gRPC?",
            rounds=2,
            stances=[Stance.FOR, Stance.AGAINST],
        )
    )
    prompts = _prompts(runner)
    assert any("Argue in favor" in prompt for prompt in prompts)  # round one steering
    assert any("Argue against" in prompt for prompt in prompts)
    assert any("Keep arguing in favor" in prompt for prompt in prompts)  # stance persists into round two
    assert any("Keep arguing against" in prompt for prompt in prompts)


async def test_no_synthesize_skips_the_closing_pass() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=1, synthesize=False)
    )
    assert result.final is None
    assert len(runner.calls) == 2  # two voices, one round, no closing delegation


async def test_needs_at_least_two_targets() -> None:
    runner = FakeProcessRunner()
    service = _debate([FakeAdapter("a")], runner)
    with pytest.raises(RutherfordError, match="at least two targets"):
        await service.debate(DebateRequest(targets=[Target(cli="a")], prompt="q"))


async def test_rounds_capped_by_config() -> None:
    runner = FakeProcessRunner()
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner, RutherfordConfig(max_debate_rounds=2))
    with pytest.raises(RutherfordError, match="max_debate_rounds"):
        await service.debate(DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=3))


async def test_target_cap_enforced() -> None:
    runner = FakeProcessRunner()
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner, RutherfordConfig(max_targets=1))
    with pytest.raises(RutherfordError) as info:
        await service.debate(DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    assert info.value.code == "TOO_MANY_TARGETS"


async def test_stance_count_must_match_targets() -> None:
    runner = FakeProcessRunner()
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    with pytest.raises(RutherfordError, match="stances"):
        await service.debate(
            DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", stances=[Stance.FOR])
        )


async def test_progress_announces_each_round() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    lines: list[str] = []
    await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2),
        on_progress=lines.append,
    )
    assert any("round 1 of 2" in line for line in lines)
    assert any("round 2 of 2" in line for line in lines)
    assert any("synthesizing" in line for line in lines)


async def test_explicit_target_labels_key_the_transcript() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(
            targets=[Target(cli="a", label="proposer"), Target(cli="b", label="critic")],
            prompt="q",
            rounds=1,
        )
    )
    assert {c.label for c in result.rounds[0].contributions} == {"proposer", "critic"}


async def test_all_voices_failing_round_one_yields_no_final() -> None:
    runner = FakeProcessRunner()
    service = _debate([FakeAdapter("a", installed=False), FakeAdapter("b", installed=False)], runner)
    result = await service.debate(DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2))
    assert len(result.rounds) == 1  # nobody survived to argue a second round
    assert all(not c.ok for c in result.rounds[0].contributions)
    assert result.final is None


async def test_duplicate_seat_labels_do_not_collide() -> None:
    # The fixed bug: two unlabeled same-(cli, model) seats share a display label, so keying identity
    # on the label merged them into one survivor and fed each the wrong "own previous position".
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="my position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="a"), Target(cli="b")], prompt="q", rounds=2)
    )
    round_one = result.rounds[0].contributions
    assert {c.label for c in round_one} == {"a", "a#2", "b"}  # disambiguated, not two "a"
    assert len({c.seat_id for c in round_one}) == 3  # three distinct identities
    assert len(result.rounds[1].contributions) == 3  # all three survive independently
    # A later-round rebuttal shows the duplicate twin as a separate participant's position.
    later_prompts = [spec.argv[2] for spec, _ in runner.calls][3:]
    assert any("## a#2" in prompt for prompt in later_prompts)


async def test_closing_uses_a_named_judge() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="closing"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("j")], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=1, judge=Target(cli="j"))
    )
    assert result.final == "closing"
    assert result.synthesis_by == "j"  # an independent, non-participant judge wrote the closing
    assert any(spec.argv[0] == "j" for spec, _ in runner.calls)


async def test_closing_defaults_to_a_participant_and_records_it() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=1))
    # No judge: a participant writes the closing, but synthesis_by surfaces which one.
    assert result.synthesis_by in {"a", "b"}


async def test_explicit_label_colliding_with_a_generated_suffix_stays_distinct() -> None:
    # The bulletproofing fix: a caller who hand-labels a seat "a#2" must not collide with the
    # auto-generated "#2" for two unlabeled "a" seats. All display labels stay distinct, and no
    # rebuttal prompt shows two different seats under one "## a#2" heading.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="my position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(
            targets=[Target(cli="a"), Target(cli="a"), Target(cli="a", label="a#2")],
            prompt="q",
            rounds=2,
        )
    )
    round_one = result.rounds[0].contributions
    assert len({c.label for c in round_one}) == 3  # three distinct display labels, no collision
    assert len({c.seat_id for c in round_one}) == 3
    later_prompts = [spec.argv[2] for spec, _ in runner.calls][3:]
    assert all(prompt.count("## a#2") <= 1 for prompt in later_prompts)


async def test_closing_with_a_failing_judge_records_no_author() -> None:
    # The bulletproofing fix: a named judge that cannot run produces no synthesis, so synthesis_by
    # must not claim the judge authored one that does not exist.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("j", installed=False)], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=1, judge=Target(cli="j"))
    )
    assert result.final is None
    assert result.synthesis_by is None


# --- F8a: time-budget harvest (round-boundary) + effort -----------------------------------------


class _RoundAwareRunner:
    """A runner where round 1 (the bare question) is instant but a rebuttal round (``Critique`` in the
    prompt) sleeps ``slow_s``. Drives the F8a deadline deterministically: round 1 completes within any
    budget, and a later round's turns overrun a small budget and are cut by ``asyncio.wait``.
    """

    def __init__(self, slow_s: float) -> None:
        self.slow_s = slow_s

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        prompt = spec.argv[2] if len(spec.argv) > 2 else ""
        await asyncio.sleep(self.slow_s if "Critique" in prompt else 0.0)  # rebuttal rounds are the slow ones
        return ProcessResult(exit_code=0, stdout=f"{spec.argv[0]} position")


class _PerCliDelayRunner:
    """A runner with per-cli delays (and optional per-cli partial lines streamed before the delay), so
    within one round a fast turn finishes while a slow one is cut -- and the cut one can have streamed a
    partial first."""

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
        return ProcessResult(exit_code=0, stdout=f"{cli} position")


def _delay_debate(runner: object, config: RutherfordConfig | None = None, clis: tuple[str, ...] = ("a", "b")):
    cfg = config or RutherfordConfig()
    registry = AdapterRegistry([FakeAdapter(cli) for cli in clis])
    delegation = DelegationService(registry, runner, cfg, load_roles())  # type: ignore[arg-type]
    return DebateService(delegation, cfg)


async def test_time_budget_finalizes_after_a_completed_round_when_the_next_is_cut() -> None:
    # 2-where/2-behavior: round 1 (instant) completes within the budget; round 2 (a rebuttal, slow) overruns
    # the remaining budget and its turns are cut by asyncio.wait. The fully-cut round 2 is KEPT (its turns
    # carry recovered sessions/traces, 2-I/2-F), but the closing + quorum run over the last USABLE round
    # (round 1). The requested 3 rounds do not all run.
    runner = _RoundAwareRunner(5.0)
    service = _delay_debate(runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=3, time_budget_s=0.3)
    )
    assert len(result.rounds) == 2  # round 1 (complete) + the kept fully-cut round 2; round 3 never ran
    assert all(c.ok for c in result.rounds[0].contributions)  # round 1 completed in full
    assert all(
        not c.ok and c.error is not None and c.error.code == "BUDGET_EXHAUSTED" for c in result.rounds[1].contributions
    )
    assert result.stop_reason == "budget"
    assert result.rollup is not None
    assert result.rollup.stop_reason == "budget"
    # answered/usable are over the last usable round (round 1); cut counts round 2's cancelled turns.
    assert result.rollup.requested == 2 and result.rollup.answered == 2 and result.rollup.usable == 2
    assert result.rollup.cut == 2 and result.rollup.quorum_met is True


async def test_a_partially_cut_round_keeps_the_turns_that_finished() -> None:
    # 2-where: within a round the budget cuts only the in-flight turns. A fast voice finishes and is kept;
    # a slow one is cut (BUDGET_EXHAUSTED) -- the round is finalized with the turn that completed.
    runner = _PerCliDelayRunner({"a": 0.0, "b": 5.0})
    service = _delay_debate(runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, time_budget_s=0.3)
    )
    assert len(result.rounds) == 1
    by_label = {c.label: c for c in result.rounds[0].contributions}
    assert by_label["a"].ok  # the fast turn finished and is kept
    assert not by_label["b"].ok and by_label["b"].error is not None
    assert by_label["b"].error.code == "BUDGET_EXHAUSTED"  # the in-flight turn was cut at the deadline
    assert result.stop_reason == "budget"
    assert result.rollup is not None
    assert result.rollup.answered == 1 and result.rollup.cut == 1 and result.rollup.usable == 1


async def test_a_cut_debate_turn_preserves_its_streamed_partial_as_a_trace() -> None:
    # 2-F (capture always): a turn cut at the deadline is a failed contribution, but the stdout it streamed
    # before the cut is preserved on the contribution's ``partial`` (a trace, never promoted to ``text``).
    runner = _PerCliDelayRunner({"a": 0.0, "b": 5.0}, partials={"b": ["b half-formed rebuttal"]})
    service = _delay_debate(runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, time_budget_s=0.3)
    )
    cut = next(c for c in result.rounds[0].contributions if c.label == "b")
    assert not cut.ok
    assert cut.partial is not None and "b half-formed rebuttal" in cut.partial
    assert "b half-formed rebuttal" not in cut.text  # a cut turn's partial is a trace, not its position


class _RoundAwarePartialRunner:
    """Round 1 (the bare question) is instant; a rebuttal round (``Critique`` in the prompt) streams a
    partial then overruns ``slow_s`` -- so a whole later round can be cut while each turn streamed a
    session-bearing partial first."""

    def __init__(self, slow_s: float, partial: str) -> None:
        self.slow_s = slow_s
        self.partial = partial

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        prompt = spec.argv[2] if len(spec.argv) > 2 else ""
        if "Critique" in prompt:  # a rebuttal round -- stream a partial, then overrun the budget
            if on_stdout is not None:
                on_stdout(self.partial)
            await asyncio.sleep(self.slow_s)
        return ProcessResult(exit_code=0, stdout=f"{spec.argv[0]} position")


async def test_a_fully_cut_round_is_kept_with_its_recovered_sessions() -> None:
    # 2-I/2-F edge: when an ENTIRE later round is cut (no turn finished) but each turn streamed a
    # session-bearing partial, the round is kept (not dropped) so its recovered resume handles and traces
    # survive into the result; the debate still finalizes over the last usable round (round 1).
    runner = _RoundAwarePartialRunner(5.0, "rebuttal streamed a session then was cut")
    service = _delay_debate(runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=3, time_budget_s=0.3)
    )
    assert len(result.rounds) == 2  # round 1 (usable) + the kept fully-cut round 2
    round_two = result.rounds[1].contributions
    assert all(not c.ok for c in round_two)  # the whole round was cut
    assert all(c.session_id == "fake-session" for c in round_two)  # ...but every handle is recovered (2-I)
    assert all(c.partial and "streamed a session" in c.partial for c in round_two)  # ...and the trace kept (2-F)


async def test_a_cut_debate_turn_recovers_its_resume_session_from_the_partial() -> None:
    # 2-I passive (debate): a turn cut mid-stream whose partial established a session records that handle on
    # its contribution (the FakeAdapter is TEXT/partial-output and parses a session), so the parent roster
    # can preserve it for a later continuation -- even though the cut turn is a trace, not a stance.
    runner = _PerCliDelayRunner({"a": 0.0, "b": 5.0}, partials={"b": ["b streamed a session then was cut"]})
    service = _delay_debate(runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, time_budget_s=0.3)
    )
    cut = next(c for c in result.rounds[0].contributions if c.label == "b")
    assert not cut.ok  # a trace cut, no usable stance
    assert cut.session_id == "fake-session"  # ...but the resume handle is recovered from the partial (2-I)


async def test_on_budget_continue_runs_every_round() -> None:
    # 2-M: with on_budget="continue" the budget is advisory -- every requested round runs to completion
    # even though a rebuttal round outlasts the budget, and the run is not flagged as a harvest.
    runner = _RoundAwareRunner(0.1)
    service = _delay_debate(runner)
    result = await service.debate(
        DebateRequest(
            targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, time_budget_s=0.01, on_budget="continue"
        )
    )
    assert len(result.rounds) == 2  # both rounds ran; the budget did not cut the debate short
    assert all(c.ok for round_ in result.rounds for c in round_.contributions)
    assert result.stop_reason is None
    assert result.rollup is not None and result.rollup.stop_reason == "ok"


async def test_debate_budget_below_quorum_raises_budget_exhausted() -> None:
    # 2-E': when even round 1 is cut so the debate yields no usable position (here both turns are slow and
    # cut at the deadline), it is a genuine BUDGET_EXHAUSTED failure raised before any result.
    runner = _PerCliDelayRunner({"a": 5.0, "b": 5.0})
    service = _delay_debate(runner)
    with pytest.raises(RutherfordError) as info:
        await service.debate(
            DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, time_budget_s=0.3)
        )
    assert info.value.code == "BUDGET_EXHAUSTED"


async def test_no_budget_means_no_rollup_for_a_debate() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, synthesize=False)
    )
    assert result.stop_reason is None
    assert result.rollup is None


async def test_effort_flows_to_every_debate_turn() -> None:
    # 2-L: the debate's effort cap reaches every turn and is reported per contribution.
    from rutherford.domain.enums import Effort

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    service = _debate([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.debate(
        DebateRequest(
            targets=[Target(cli="a"), Target(cli="b")], prompt="q", rounds=2, synthesize=False, effort=Effort.HIGH
        )
    )
    contributions = [c for round_ in result.rounds for c in round_.contributions]
    assert contributions and all(c.effort_applied == Effort.HIGH for c in contributions)
    assert all("--effort=high" in spec.argv for spec, _ in runner.calls)
