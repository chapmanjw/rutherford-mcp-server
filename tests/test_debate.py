# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the debate service, driven by fakes."""

from __future__ import annotations

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import Stance
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DebateRequest, ProcessResult, Target
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
