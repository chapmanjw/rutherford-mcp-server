# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the consensus service, driven by fakes."""

from __future__ import annotations

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import Stance
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ConsensusRequest, ProcessResult, Target
from rutherford.services.consensus import ConsensusService
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


def _consensus(adapters: list[FakeAdapter], runner: FakeProcessRunner, config: RutherfordConfig | None = None):
    cfg = config or RutherfordConfig()
    delegation = DelegationService(AdapterRegistry(adapters), runner, cfg, load_roles())
    return ConsensusService(delegation, cfg)


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
