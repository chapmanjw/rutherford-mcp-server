# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the consensus service, driven by fakes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import AuthState, Stance, Strategy
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import (
    ConsensusRequest,
    ConsensusResult,
    DelegationRequest,
    InvocationSpec,
    ProcessResult,
    StrategyResult,
    Target,
)
from rutherford.services.consensus import ConsensusService
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


def _consensus(adapters: list[FakeAdapter], runner: FakeProcessRunner, config: RutherfordConfig | None = None):
    cfg = config or RutherfordConfig()
    registry = AdapterRegistry(adapters)
    delegation = DelegationService(registry, runner, cfg, load_roles())
    return ConsensusService(delegation, cfg, registry)


async def test_one_voice_per_target() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="best language?")
    )
    assert len(result.voices) == 2
    assert {voice.target.cli for voice in result.voices} == {"a", "b"}
    assert all(voice.ok for voice in result.voices)
    assert result.synthesis is None  # off by default


async def test_one_bad_voice_does_not_abort_the_panel() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    # "b" is not installed -> its voice fails, "a" still answers.
    service = _consensus([FakeAdapter("a"), FakeAdapter("b", installed=False)], runner)
    result = await service.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["a"].ok
    assert not by_cli["b"].ok
    assert by_cli["b"].error is not None
    assert by_cli["b"].error.code == "BINARY_NOT_FOUND"


async def test_a_raising_adapter_probe_does_not_abort_the_panel() -> None:
    # The headline promise under its harshest test: an adapter whose detect() RAISES (not
    # "returns a failure") must become one structured failed voice while the siblings answer.
    class _DetectRaises(FakeAdapter):
        def detect(self):
            raise RuntimeError("probe exploded")

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), _DetectRaises("boom"), FakeAdapter("c")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="boom"), Target(cli="c")], prompt="q")
    )
    assert isinstance(result, ConsensusResult)
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["a"].ok and by_cli["c"].ok  # the healthy siblings still answered
    assert not by_cli["boom"].ok
    assert by_cli["boom"].error is not None
    assert by_cli["boom"].error.code == "INTERNAL"


async def test_a_cancellation_escaping_a_voice_propagates_not_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The flip side of the exception fold: gather(return_exceptions=True) captures CancelledError
    # too, so without the explicit re-raise a cancelled panel would be SWALLOWED into one INTERNAL
    # failed voice and the consensus would "complete". Cancellation must stay cancellation.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    real_delegate = DelegationService.delegate

    async def cancelled(self: DelegationService, req: DelegationRequest, **kwargs: object):
        if req.target.cli == "b":
            raise asyncio.CancelledError()
        return await real_delegate(self, req, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DelegationService, "delegate", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))


async def test_an_exception_escaping_delegate_is_folded_into_a_failed_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense in depth behind the delegate()-level containment: even if an exception still gets
    # OUT of delegate(), the fan-out folds it into that voice instead of failing the gather.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    real_delegate = DelegationService.delegate

    async def explode(self: DelegationService, req: DelegationRequest, **kwargs: object):
        if req.target.cli == "b":
            raise RuntimeError("escaped containment")
        return await real_delegate(self, req, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DelegationService, "delegate", explode)
    result = await service.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["a"].ok
    assert not by_cli["b"].ok
    assert by_cli["b"].error is not None
    assert by_cli["b"].error.code == "INTERNAL"
    assert "escaped containment" in by_cli["b"].error.message


async def test_stances_steer_each_prompt() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b")],
            prompt="rewrite in Rust?",
            stances=[Stance.FOR, Stance.AGAINST],
        )
    )
    prompts = [spec.argv[2] for spec, _ in runner.calls]
    assert any("Argue in favor" in prompt for prompt in prompts)
    assert any("Argue against" in prompt for prompt in prompts)


async def test_per_target_stance_steers_that_voice() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a", stance=Stance.FOR), Target(cli="b", stance=Stance.AGAINST)],
            prompt="adopt gRPC?",
        )
    )
    prompts = [spec.argv[2] for spec, _ in runner.calls]
    assert any("Argue in favor" in prompt for prompt in prompts)
    assert any("Argue against" in prompt for prompt in prompts)


async def test_per_target_role_overrides_call_role(tmp_path: Path) -> None:
    (tmp_path / "sleuth.md").write_text("---\nname: sleuth\n---\nBE A SLEUTH.\n", encoding="utf-8")
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    registry = AdapterRegistry([FakeAdapter("a"), FakeAdapter("b")])
    delegation = DelegationService(registry, runner, RutherfordConfig(), load_roles(extra_dirs=[tmp_path]))
    service = ConsensusService(delegation, RutherfordConfig(), registry)
    await service.consensus(
        ConsensusRequest(targets=[Target(cli="a", role="sleuth"), Target(cli="b")], prompt="who did it?", role=None)
    )
    by_cli = {spec.argv[0]: spec.argv[2] for spec, _ in runner.calls}
    assert "BE A SLEUTH." in by_cli["a"]  # the per-target role was applied to its voice
    assert "BE A SLEUTH." not in by_cli["b"]  # and only to its voice


async def test_target_cap_enforced() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner, RutherfordConfig(max_targets=1))
    with pytest.raises(RutherfordError) as info:
        await service.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    assert info.value.code == "TOO_MANY_TARGETS"


async def test_stance_count_must_match_targets() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    with pytest.raises(RutherfordError, match="stances"):
        await service.consensus(
            ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", stances=[Stance.FOR])
        )


async def test_empty_targets_rejected() -> None:
    runner = FakeProcessRunner()
    with pytest.raises(RutherfordError, match="at least one target"):
        await _consensus([FakeAdapter("a")], runner).consensus(ConsensusRequest(targets=[], prompt="q"))


async def test_synthesize_produces_a_combined_answer() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="combined answer"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", synthesize=True)
    )
    assert result.synthesis == "combined answer"
    # Two voices plus one synthesis delegation.
    assert len(runner.calls) == 3


async def test_explicit_synthesize_false_overrides_synthesize_default() -> None:
    # F13: synthesize is tri-state. An explicit False must win over synthesize_default=true --
    # before the fix the gate OR'd the two, so a caller could never turn the configured default off.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner, RutherfordConfig(synthesize_default=True))
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", synthesize=False)
    )
    assert result.synthesis is None
    assert len(runner.calls) == 2  # two voices only; no synthesis delegation ran


async def test_omitted_synthesize_defers_to_synthesize_default() -> None:
    # The None sentinel: an omitted synthesize is the one case the configured default fills.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="combined answer"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner, RutherfordConfig(synthesize_default=True))
    result = await service.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    assert result.synthesis == "combined answer"
    assert len(runner.calls) == 3  # two voices plus the synthesis delegation


async def test_synthesis_uses_a_named_judge_target() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("j")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", synthesize=True, judge=Target(cli="j"))
    )
    assert result.synthesis is not None
    assert result.synthesis_by == "j"  # the named non-participant judge wrote it
    assert any(spec.argv[0] == "j" for spec, _ in runner.calls)  # synthesis was delegated to the judge


async def test_synthesis_defaults_to_first_voice_and_records_it() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", synthesize=True)
    )
    # With no judge, a participant synthesizes -- but who is now surfaced, not hidden.
    assert result.synthesis_by == "a"


async def test_synthesis_with_a_failing_judge_records_no_author() -> None:
    # The bulletproofing fix: a named judge that cannot run produces no synthesis, so synthesis_by
    # must be None rather than claiming the absent judge wrote one.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("j", installed=False)], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", synthesize=True, judge=Target(cli="j"))
    )
    assert result.synthesis is None
    assert result.synthesis_by is None


# --- consensus strategies -----------------------------------------------------------------------


def _verdict_runner(verdicts: dict[str, str]) -> FakeProcessRunner:
    """A runner where each cli answers with its mapped ``VERDICT:`` token."""

    def run_fn(spec: object) -> ProcessResult:
        cli = spec.argv[0]  # type: ignore[attr-defined]
        return ProcessResult(exit_code=0, stdout=f"my reasoning\nVERDICT: {verdicts[cli]}")

    return FakeProcessRunner(run_fn=run_fn)


async def test_no_strategy_returns_the_legacy_consensus_shape() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.consensus(ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q"))
    assert isinstance(result, ConsensusResult)  # not a StrategyResult


async def test_strategy_appends_a_verdict_instruction_to_each_voice() -> None:
    runner = _verdict_runner({"a": "yes", "b": "yes"})
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", strategy=Strategy.UNANIMOUS)
    )
    prompts = [spec.argv[2] for spec, _ in runner.calls]
    assert all("VERDICT:" in prompt for prompt in prompts)


async def test_majority_strategy_returns_the_winning_verdict() -> None:
    runner = _verdict_runner({"a": "approve", "b": "approve", "c": "block"})
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")], runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b"), Target(cli="c")],
            prompt="ship it?",
            strategy=Strategy.MAJORITY,
        )
    )
    assert isinstance(result, StrategyResult)
    assert result.outcome == "majority"
    assert result.decision == "approve"
    assert {voice.cli: voice.verdict for voice in result.voices} == {"a": "approve", "b": "approve", "c": "block"}


async def test_weighted_strategy_lets_a_heavy_voice_win() -> None:
    runner = _verdict_runner({"a": "approve", "b": "approve", "c": "block"})
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")], runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b"), Target(cli="c", weight=5.0)],
            prompt="ship it?",
            strategy=Strategy.WEIGHTED,
        )
    )
    assert isinstance(result, StrategyResult)
    assert result.decision == "block"  # the heavy "block" outweighs two "approve" votes


async def test_unparseable_voice_is_returned_and_vetoes_unanimity() -> None:
    def run_fn(spec: object) -> ProcessResult:
        cli = spec.argv[0]  # type: ignore[attr-defined]
        if cli == "c":
            return ProcessResult(exit_code=0, stdout="I won't commit to a verdict.")
        return ProcessResult(exit_code=0, stdout="reasoning\nVERDICT: approve")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")], runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b"), Target(cli="c")],
            prompt="q",
            strategy=Strategy.UNANIMOUS,
        )
    )
    assert isinstance(result, StrategyResult)
    # The fixed bug: a voice that did not produce a verdict vetoes unanimity rather than being
    # silently excluded so the survivors certify "unanimous".
    assert result.outcome == "split"
    by_cli = {voice.cli: voice for voice in result.voices}
    assert by_cli["c"].verdict is None  # still returned
    assert by_cli["c"].no_verdict_reason == "unparseable"  # and the reason is recorded, not silent
    assert by_cli["c"].text == "I won't commit to a verdict."


async def test_strategy_records_failed_voice_reason() -> None:
    # A failed voice is kept in the tally denominator with reason "failed", so an outcome is never
    # certified off a minority of survivors without a trace of who dropped out.
    runner = _verdict_runner({"a": "approve", "b": "approve"})
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c", installed=False)], runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b"), Target(cli="c")],
            prompt="q",
            strategy=Strategy.MAJORITY,
        )
    )
    assert isinstance(result, StrategyResult)
    assert result.outcome == "majority" and result.decision == "approve"  # 2 of 3 eligible
    by_cli = {voice.cli: voice for voice in result.voices}
    assert by_cli["c"].ok is False
    assert by_cli["c"].no_verdict_reason == "failed"


# --- (a) expand_all: fan out to every installed + authenticated adapter -------------------------


async def test_expand_all_fans_out_to_authenticated_adapters() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    adapters = [
        FakeAdapter("a", auth_state=AuthState.AUTHENTICATED),
        FakeAdapter("b", auth_state=AuthState.NEEDS_LOGIN),
        FakeAdapter("c", auth_state=AuthState.UNKNOWN),  # no cheap check -> included optimistically
        FakeAdapter("d", installed=False),
    ]
    service = _consensus(adapters, runner)
    result = await service.consensus(ConsensusRequest(prompt="which language?", expand_all=True))

    assert {voice.target.cli for voice in result.voices} == {"a", "c"}
    assert all(voice.target.model is None for voice in result.voices)  # each at its default model
    skipped = {entry.cli: entry.reason for entry in result.skipped}
    assert set(skipped) == {"b", "d"}
    assert "not installed" in skipped["d"]


async def test_expand_all_excludes_optional_adapters() -> None:
    # An optional adapter (a local model) is installed + authenticated, but must NOT auto-join the
    # "all" panel -- it only participates when named explicitly, so it never silently slows it.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    adapters = [FakeAdapter("a"), FakeAdapter("ollama", optional=True)]
    service = _consensus(adapters, runner)
    result = await service.consensus(ConsensusRequest(prompt="q", expand_all=True))

    assert {voice.target.cli for voice in result.voices} == {"a"}
    skipped = {entry.cli: entry.reason for entry in result.skipped}
    assert "ollama" in skipped
    assert "optional" in skipped["ollama"]


async def test_expand_all_announces_panel_via_progress() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    adapters = [FakeAdapter("a"), FakeAdapter("b", auth_state=AuthState.NEEDS_LOGIN)]
    service = _consensus(adapters, runner)
    lines: list[str] = []
    await service.consensus(ConsensusRequest(prompt="q", expand_all=True), on_progress=lines.append)
    assert any("including a" in line for line in lines)
    assert any("skipping b" in line for line in lines)


async def test_expand_all_caps_at_max_targets() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    adapters = [FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")]
    service = _consensus(adapters, runner, RutherfordConfig(max_targets=2))
    result = await service.consensus(ConsensusRequest(prompt="q", expand_all=True))
    assert len(result.voices) == 2
    assert any("max_targets" in entry.reason for entry in result.skipped)


async def test_expand_all_rejects_stances() -> None:
    runner = FakeProcessRunner()
    service = _consensus([FakeAdapter("a")], runner)
    with pytest.raises(RutherfordError, match="stances cannot be combined"):
        await service.consensus(ConsensusRequest(prompt="q", expand_all=True, stances=[Stance.FOR]))


# --- (b) a voice whose named model is unavailable falls back instead of being dropped -----------


async def test_consensus_voice_falls_back_on_unavailable_model() -> None:
    def run_fn(spec: object) -> ProcessResult:
        argv = spec.argv  # type: ignore[attr-defined]
        if "named-only" in argv:
            return ProcessResult(exit_code=1, stderr="Named models unavailable on your plan. Switch to Auto.")
        return ProcessResult(exit_code=0, stdout="answered on auto")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _consensus([FakeAdapter("a"), FakeAdapter("c", fallback_model="auto")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="c", model="named-only")], prompt="q")
    )
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["a"].ok
    assert by_cli["c"].ok  # the voice survived rather than being dropped
    assert by_cli["c"].text == "answered on auto"
    assert by_cli["c"].fallback_from == "named-only"  # surfaced: a fallback occurred
    assert by_cli["c"].target.model == "auto"  # surfaced: the model that actually answered


# --- (c) a hard-failing adapter is reported but does not sink the panel -------------------------


async def test_expand_all_one_hard_failure_does_not_sink_panel() -> None:
    def run_fn(spec: object) -> ProcessResult:
        if spec.argv[0] == "b":  # type: ignore[attr-defined]
            return ProcessResult(exit_code=2, stderr="boom: internal error")
        return ProcessResult(exit_code=0, stdout="ok")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _consensus([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")], runner)
    result = await service.consensus(ConsensusRequest(prompt="q", expand_all=True))

    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["a"].ok and by_cli["c"].ok  # other voices still return
    assert not by_cli["b"].ok
    assert by_cli["b"].error is not None
    assert by_cli["b"].error.code == "NONZERO_EXIT"  # explicit failure, not an empty voice
    assert by_cli["b"].text == ""


async def test_expand_all_with_nothing_authenticated_returns_empty_panel() -> None:
    runner = FakeProcessRunner()
    adapters = [FakeAdapter("a", auth_state=AuthState.NEEDS_LOGIN), FakeAdapter("b", installed=False)]
    service = _consensus(adapters, runner)
    result = await service.consensus(ConsensusRequest(prompt="q", expand_all=True))
    assert result.voices == []
    assert {entry.cli for entry in result.skipped} == {"a", "b"}
    assert result.synthesis is None


# --- (d) the global concurrency cap bounds parallel fan-out -------------------------------------


class _ConcurrencyRunner:
    """A ProcessRunner with a real await point, so a test can observe how many runs overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return ProcessResult(exit_code=0, stdout="ok")


async def test_max_concurrency_bounds_parallel_fan_out() -> None:
    runner = _ConcurrencyRunner()
    cfg = RutherfordConfig(max_concurrency=2)
    registry = AdapterRegistry([FakeAdapter(f"a{i}") for i in range(4)])
    service = ConsensusService(DelegationService(registry, runner, cfg, load_roles()), cfg, registry)
    result = await service.consensus(ConsensusRequest(targets=[Target(cli=f"a{i}") for i in range(4)], prompt="q"))
    assert len(result.voices) == 4 and all(voice.ok for voice in result.voices)
    assert runner.max_active <= 2  # the global semaphore capped concurrent subprocesses (4 -> 2)


async def test_one_shared_cap_bounds_concurrent_consensus_and_debate() -> None:
    # The headline F9 property: ONE semaphore on the shared DelegationService bounds concurrency
    # across a consensus AND a debate running at once -- not a per-call cap. A future refactor that
    # moved the semaphore into a per-call object would let this exceed the cap and fail here.
    from rutherford.domain.models import DebateRequest
    from rutherford.services.debate import DebateService

    runner = _ConcurrencyRunner()
    cfg = RutherfordConfig(max_concurrency=2)
    registry = AdapterRegistry([FakeAdapter(f"a{i}") for i in range(4)])
    delegation = DelegationService(registry, runner, cfg, load_roles())
    consensus = ConsensusService(delegation, cfg, registry)
    debate = DebateService(delegation, cfg)
    await asyncio.gather(
        consensus.consensus(ConsensusRequest(targets=[Target(cli=f"a{i}") for i in range(4)], prompt="q")),
        debate.debate(
            DebateRequest(targets=[Target(cli="a0"), Target(cli="a1")], prompt="q", rounds=2, synthesize=False)
        ),
    )
    assert runner.max_active <= 2  # both panels share one cap


# --- (e) F8a: time-budget harvest + effort ------------------------------------------------------


class _BudgetRunner:
    """A runner with per-cli delays that streams optional partial lines before its delay.

    Drives the time-budget harvest deterministically: a zero-delay voice finishes within any budget;
    a long-delay voice is still in-flight at the deadline and gets cut. Partial lines are teed to
    ``on_stdout`` *before* the delay, so a cut voice has accumulated a partial answer at the cut.
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


def _budget_consensus(runner: object, config: RutherfordConfig | None = None) -> ConsensusService:
    cfg = config or RutherfordConfig()
    registry = AdapterRegistry([FakeAdapter("fast"), FakeAdapter("slow"), FakeAdapter("slow2")])
    delegation = DelegationService(registry, runner, cfg, load_roles())  # type: ignore[arg-type]
    return ConsensusService(delegation, cfg, registry)


async def test_time_budget_harvests_fast_voices_and_cuts_slow_ones() -> None:
    # The headline F8a behavior (2-behavior): at the deadline the answered voices are kept and the
    # in-flight ones are cut, with the panel still returning a result.
    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert isinstance(result, ConsensusResult)
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["fast"].ok and by_cli["fast"].text == "fast answered"
    assert not by_cli["slow"].ok  # cut at the deadline
    assert by_cli["slow"].error is not None
    assert by_cli["slow"].error.code == "BUDGET_EXHAUSTED"
    assert by_cli["slow"].stop_reason == "budget"
    assert result.stop_reason == "budget"


async def test_budget_harvest_promotes_a_partial_to_a_usable_candidate_answer() -> None:
    # 2-F/2-H: a cut voice on a supports_partial_output adapter (FakeAdapter is TEXT) has the stdout it
    # streamed before the cut harvested through the adapter's parser into a usable candidate answer -- so
    # the in-flight work is not wasted: it is ``ok``, counts toward quorum, and feeds aggregation/synthesis.
    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0}, partials={"slow": ["draft line 1", "draft line 2"]})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert isinstance(result, ConsensusResult)
    slow = next(voice for voice in result.voices if voice.target.cli == "slow")
    assert slow.ok  # the streamed partial became a usable candidate answer
    assert slow.stop_reason == "budget"  # ...but flagged as a budget harvest, not a clean finish
    assert "draft line 1" in slow.text and "draft line 2" in slow.text  # the partial is the answer
    assert slow.partial is not None and "draft line 1" in slow.partial  # raw bytes preserved too
    assert result.rollup is not None and result.rollup.usable == 2  # both voices count toward quorum


async def test_harvested_partial_reports_the_resolved_default_effort() -> None:
    # The harvested partial must report the effort the subprocess actually ran with -- which, when the call
    # named none, is the configured default_effort (resolved like the delegation service does), not None.
    from rutherford.domain.enums import Effort

    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0}, partials={"slow": ["partial draft"]})
    registry = AdapterRegistry([FakeAdapter("fast"), FakeAdapter("slow")])
    cfg = RutherfordConfig(default_effort=Effort.HIGH)  # the call names no effort; the default fills it
    service = ConsensusService(DelegationService(registry, runner, cfg, load_roles()), cfg, registry)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert isinstance(result, ConsensusResult)
    slow = next(voice for voice in result.voices if voice.target.cli == "slow")
    assert slow.ok  # the partial was harvested into a usable answer
    assert slow.effort == Effort.HIGH and slow.effort_applied == Effort.HIGH  # the resolved default, not None


async def test_budget_cut_on_a_single_envelope_adapter_is_a_trace_not_an_answer() -> None:
    # 2-H: a single-envelope adapter (JSON/TRANSCRIPT) emits its answer once, at the end, so a cut yields
    # only a partial TRACE, never a usable answer. Such a cut voice stays a BUDGET_EXHAUSTED failure with
    # the raw bytes preserved on ``partial`` -- it must NOT be promoted to a usable answer.
    from rutherford.domain.enums import OutputMode
    from rutherford.domain.models import AdapterCapabilities

    class _SingleEnvelopeAdapter(FakeAdapter):
        def capabilities(self) -> AdapterCapabilities:
            return AdapterCapabilities(output_mode=OutputMode.JSON)  # supports_partial_output -> False

    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0}, partials={"slow": ["incomplete trace"]})
    registry = AdapterRegistry([FakeAdapter("fast"), _SingleEnvelopeAdapter("slow")])
    cfg = RutherfordConfig()
    service = ConsensusService(DelegationService(registry, runner, cfg, load_roles()), cfg, registry)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert isinstance(result, ConsensusResult)
    slow = next(voice for voice in result.voices if voice.target.cli == "slow")
    assert not slow.ok  # a single-envelope cut is a trace, never a usable answer
    assert slow.error is not None and slow.error.code == "BUDGET_EXHAUSTED"
    assert slow.partial is not None and "incomplete trace" in slow.partial  # kept as a trace
    assert result.rollup is not None and result.rollup.usable == 1  # only the fast voice counts


async def test_budget_below_quorum_raises_budget_exhausted() -> None:
    # 2-E': a harvest that left fewer than min_quorum usable voices is a genuine failure raised before
    # any result is returned -- the zero/under-yield edge BUDGET_EXHAUSTED is reserved for.
    runner = _BudgetRunner({"slow": 5.0, "slow2": 5.0})
    service = _budget_consensus(runner, RutherfordConfig(min_quorum=2))
    with pytest.raises(RutherfordError) as info:
        await service.consensus(
            ConsensusRequest(targets=[Target(cli="slow"), Target(cli="slow2")], prompt="q", time_budget_s=0.3)
        )
    assert info.value.code == "BUDGET_EXHAUSTED"
    assert "min_quorum" in info.value.message


async def test_on_budget_continue_runs_every_voice_to_completion() -> None:
    # 2-M: with on_budget="continue" the budget is advisory -- every voice runs to completion and none
    # is cut, even one slower than the budget. The rollup still records the run, with no harvest.
    runner = _BudgetRunner({"fast": 0.0, "slow": 0.2})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.05, on_budget="continue"
        )
    )
    assert all(voice.ok for voice in result.voices)  # nothing cut despite the slow voice exceeding 0.05s
    assert result.stop_reason is None
    assert result.rollup is not None
    assert result.rollup.stop_reason == "ok"
    assert result.rollup.cut == 0


async def test_on_budget_resume_cuts_the_straggler_like_harvest_today() -> None:
    # 2-M: ``resume`` cuts the stragglers at the deadline like ``harvest`` and intends a later deliberate
    # come-back to them. Today that come-back rides the item-9 continuation primitive and a mid-run-cut
    # voice has no established session to record, so ``resume`` is harvest-equivalent -- pinned here so a
    # future item-9 change that diverges them is a deliberate, visible edit.
    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3, on_budget="resume"
        )
    )
    assert isinstance(result, ConsensusResult)
    by_cli = {voice.target.cli: voice for voice in result.voices}
    assert by_cli["fast"].ok
    assert not by_cli["slow"].ok and by_cli["slow"].error is not None
    assert by_cli["slow"].error.code == "BUDGET_EXHAUSTED"  # cut at the deadline, exactly like harvest
    assert result.stop_reason == "budget"


async def test_on_budget_continue_detaches_publishing_an_interim_then_the_full_set() -> None:
    # 2-M: on_budget=continue with an interim sink (an async job) detaches at the deadline -- it publishes
    # the best-effort answered-so-far set and keeps the stragglers running, returning the full set when they
    # land. Nothing is cut: the final has every voice and is not flagged as a budget harvest.
    interims: list[ConsensusResult | StrategyResult] = []
    runner = _BudgetRunner({"fast": 0.0, "slow": 0.3})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.1, on_budget="continue"
        ),
        on_interim_result=interims.append,
    )
    assert interims, "an interim best-effort result must be published at the deadline"
    first = interims[0]
    assert isinstance(first, ConsensusResult)
    assert {voice.target.cli for voice in first.voices} == {"fast"}  # only the answered-so-far voice
    assert first.notice is not None and "still running" in first.notice
    assert isinstance(result, ConsensusResult)
    assert {voice.target.cli for voice in result.voices} == {"fast", "slow"}  # the full set at the end
    assert all(voice.ok for voice in result.voices)
    assert result.stop_reason is None  # continue never cuts -- not a harvest


async def test_on_budget_continue_without_an_interim_sink_runs_all_to_completion() -> None:
    # The sync path: with no interim sink, continue cannot detach -- it just runs every voice to completion
    # and returns the full set (no interim emitted, nothing cut).
    runner = _BudgetRunner({"fast": 0.0, "slow": 0.2})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.05, on_budget="continue"
        )
    )
    assert isinstance(result, ConsensusResult)
    assert all(voice.ok for voice in result.voices) and len(result.voices) == 2
    assert result.stop_reason is None


async def test_strategy_carries_per_voice_effort_applied() -> None:
    # 2-L: the applied-effort reporting surface must survive the strategy aggregation -- each VoiceVerdict
    # carries the tier its voice actually applied, not just the all-voices result.
    from rutherford.domain.enums import Effort

    runner = _verdict_runner({"a": "approve", "b": "approve"})
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a"), Target(cli="b")], prompt="q", strategy=Strategy.MAJORITY, effort=Effort.HIGH
        )
    )
    assert isinstance(result, StrategyResult)
    assert result.voices and all(verdict.effort_applied == Effort.HIGH for verdict in result.voices)


async def test_rollup_effort_requested_reflects_the_resolved_default() -> None:
    # 2-L-map: a budget that defaulted the effort must not report None for requested -- the rollup records
    # the resolved tier (the configured default the voices actually ran with).
    from rutherford.domain.enums import Effort

    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0})
    service = _budget_consensus(runner, RutherfordConfig(default_effort=Effort.HIGH))
    result = await service.consensus(  # effort omitted -> resolves to default_effort=high
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert result.rollup is not None
    assert result.rollup.effort_requested == Effort.HIGH


async def test_default_on_budget_continue_is_honored_when_the_call_omits_it() -> None:
    # 2-M (per-call param + workspace default): an omitted on_budget follows the configured
    # default_on_budget. With default_on_budget="continue", a slow voice that exceeds the budget is NOT
    # cut (the budget is advisory) -- proving the config default reached the service, not a hard "harvest".
    runner = _BudgetRunner({"fast": 0.0, "slow": 0.2})
    service = _budget_consensus(runner, RutherfordConfig(default_on_budget="continue"))
    result = await service.consensus(  # on_budget omitted -> resolves to the config default (continue)
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.05)
    )
    assert isinstance(result, ConsensusResult)
    assert all(voice.ok for voice in result.voices)  # nothing cut despite the slow voice exceeding 0.05s
    assert result.stop_reason is None


async def test_no_budget_means_no_rollup_and_no_stop_reason() -> None:
    # The default path: no time budget -> the run completes normally, with no rollup and no stop_reason,
    # so the F8a fields stay absent from the wire for the common case.
    runner = _BudgetRunner({"fast": 0.0, "slow": 0.0})
    service = _budget_consensus(runner)
    result = await service.consensus(ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q"))
    assert result.stop_reason is None
    assert result.rollup is None


async def test_budget_rollup_reports_counts_and_effort() -> None:
    # The rollup is the F8a audit surface: who was issued, who answered, who was cut, quorum, and the
    # effort actually applied (the FakeAdapter echoes the requested tier as applied).
    from rutherford.domain.enums import Effort

    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0})
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3, effort=Effort.HIGH
        )
    )
    rollup = result.rollup
    assert rollup is not None
    assert rollup.stop_reason == "budget"
    assert rollup.requested == 2
    assert rollup.answered == 1 and rollup.cut == 1
    assert rollup.usable == 1 and rollup.quorum_met is True
    assert rollup.time_budget_s == 0.3
    assert rollup.effort_requested == Effort.HIGH
    assert rollup.effort_applied == Effort.HIGH  # the fake "supports" the tier, so applied == requested
    assert rollup.elapsed_s >= 0.0


class _HarvestRunner:
    """A runner where the slow voice streams a partial then overruns, and the harvest_partial follow-up
    (its prompt contains "out of time") returns a clean best answer fast -- so a test can drive 2-I."""

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        prompt = spec.argv[2] if len(spec.argv) > 2 else ""
        if "out of time" in prompt:  # the 2-I active harvest follow-up against the recovered session
            return ProcessResult(exit_code=0, stdout="clean best answer")
        cli = spec.argv[0]
        if cli == "slow":
            if on_stdout is not None:
                on_stdout("raw partial draft")  # streamed before the cut -> recovers a session
            await asyncio.sleep(5.0)
            return ProcessResult(exit_code=0, stdout="slow answered")
        return ProcessResult(exit_code=0, stdout=f"{cli} answered")


async def test_harvest_partial_reprompts_a_cut_voice_for_a_clean_best_answer() -> None:
    # 2-I active: harvest_partial=true re-prompts a cut voice whose session was recovered (from its streamed
    # partial) for a clean best answer, which replaces the raw partial.
    runner = _HarvestRunner()
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3, harvest_partial=True
        )
    )
    assert isinstance(result, ConsensusResult)
    slow = next(voice for voice in result.voices if voice.target.cli == "slow")
    assert slow.ok
    assert slow.text == "clean best answer"  # the re-prompt replaced the raw partial
    assert slow.stop_reason == "budget"  # still flagged as a (post-deadline) harvest


async def test_cut_voice_records_a_recovered_session_even_without_an_answer() -> None:
    # 2-I passive: a voice cut mid-run whose partial established a session but no usable answer yet must
    # still record that session handle, so a later resume/harvest can use it.
    from rutherford.domain.enums import OutputMode
    from rutherford.domain.error_codes import ErrorCode
    from rutherford.domain.models import AdapterCapabilities, DelegationResult, ErrorInfo, InvocationContext

    class _SessionNoAnswerAdapter(FakeAdapter):
        def capabilities(self) -> AdapterCapabilities:
            return AdapterCapabilities(supports_resume=True, output_mode=OutputMode.JSONL)  # partial-output

        def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
            # The partial established a session but carries no answer yet -> a failed parse with the session.
            return DelegationResult(
                target=ctx.target,
                ok=False,
                error=ErrorInfo(code=ErrorCode.PARSE_ERROR, message="no answer yet"),
                session_id="recovered-sess",
                safety_mode=ctx.safety_mode,
            )

    runner = _BudgetRunner({"fast": 0.0, "slow": 5.0}, partials={"slow": ["session established, still thinking"]})
    registry = AdapterRegistry([FakeAdapter("fast"), _SessionNoAnswerAdapter("slow")])
    cfg = RutherfordConfig()
    service = ConsensusService(DelegationService(registry, runner, cfg, load_roles()), cfg, registry)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert isinstance(result, ConsensusResult)
    slow = next(voice for voice in result.voices if voice.target.cli == "slow")
    assert not slow.ok  # no usable answer -> a trace cut
    assert slow.session_id == "recovered-sess"  # ...but the session is recorded for a later resume (2-I)


async def test_harvest_partial_off_leaves_the_raw_partial() -> None:
    # Without harvest_partial, the cut voice keeps its raw harvested partial (no follow-up re-prompt).
    runner = _HarvestRunner()
    service = _budget_consensus(runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="fast"), Target(cli="slow")], prompt="q", time_budget_s=0.3)
    )
    assert isinstance(result, ConsensusResult)
    slow = next(voice for voice in result.voices if voice.target.cli == "slow")
    assert slow.text == "raw partial draft"  # the raw streamed partial, not a re-prompted answer


async def test_outer_cancellation_during_harvest_propagates_and_drains() -> None:
    # An external cancel of the whole panel (e.g. cancel_job) while voices are in flight must propagate
    # as CancelledError, not be folded into a budget-cut voice. The slow voices are cancel-drained.
    runner = _BudgetRunner({"slow": 5.0, "slow2": 5.0})
    service = _budget_consensus(runner)
    task = asyncio.ensure_future(
        service.consensus(ConsensusRequest(targets=[Target(cli="slow"), Target(cli="slow2")], prompt="q"))
    )
    await asyncio.sleep(0.05)  # let the voices start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_effort_flows_to_every_voice_and_is_reported_applied() -> None:
    # 2-L: the panel's effort cap reaches each voice, and the applied tier is reported back per voice
    # (the FakeAdapter maps every tier as supported, echoing it as applied).
    from rutherford.domain.enums import Effort

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a"), Target(cli="b")], prompt="q", effort=Effort.MEDIUM)
    )
    assert all(voice.effort == Effort.MEDIUM for voice in result.voices)
    assert all(voice.effort_applied == Effort.MEDIUM for voice in result.voices)
    # The fake adapter encodes the effort flag into the argv, so it reached the invocation.
    assert all("--effort=medium" in spec.argv for spec, _ in runner.calls)


async def test_expand_all_skips_a_benched_adapter() -> None:
    # F7: an adapter on cooldown is left out of the auto-expanded panel, with a reason.
    config = RutherfordConfig(cooldown_threshold=1)  # one unhealthy failure benches

    def run_fn(spec: InvocationSpec) -> ProcessResult:
        cli = spec.argv[0]
        if cli == "b":
            return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # unhealthy -> benches b
        return ProcessResult(exit_code=0, stdout="ok")

    runner = FakeProcessRunner(run_fn=run_fn)
    registry = AdapterRegistry([FakeAdapter("a"), FakeAdapter("b")])
    delegation = DelegationService(registry, runner, config, load_roles())
    consensus = ConsensusService(delegation, config, registry)

    await delegation.delegate(DelegationRequest(target=Target(cli="b"), prompt="q"))  # bench b
    assert delegation.is_benched("b")

    result = await consensus.consensus(ConsensusRequest(expand_all=True, prompt="q"))
    assert isinstance(result, ConsensusResult)
    included = {voice.target.cli for voice in result.voices}
    assert "a" in included
    assert "b" not in included
    assert any(entry.cli == "b" and "cooldown" in entry.reason for entry in result.skipped)
