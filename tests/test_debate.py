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
from rutherford.domain.enums import Effort
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DebateRequest, Target
from rutherford.services.debate import DebateService, _disambiguate
from rutherford.services.delegation import DelegationService
from rutherford.tools.debate import debate_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
DEAD = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
# A slow agent: streams a partial then sleeps 1.5s, so a tight round deadline cuts its turn mid-round. The
# sleep is kept small on purpose: round-boundary budget cuts work at asyncio resolution, so the slow voice
# only needs to outlast the ~1.2s cut budget by a clear margin (it finishes near subprocess-spawn + 1.5s).
# The budgets below sit near a second rather than truly sub-second because of a ~0.7s subprocess-spawn floor
# per voice: a budget under that floor would cut the FAST voice too, starving the round.
SLOW = AgentDescriptor(
    "slow", "Slow", _FAKE_CMD, default_model="model-s", env_overrides=(("RUTHERFORD_FAKE_SLEEP", "1.5"),)
)


def _service(config: RutherfordConfig | None = None) -> DebateService:
    resolved = config or RutherfordConfig()
    registry = DescriptorRegistry([FAKE, DEAD, SLOW])
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


def test_disambiguate_labels() -> None:
    assert _disambiguate(["a", "b"]) == ["a", "b"]
    assert _disambiguate(["a", "a", "b"]) == ["a#1", "a#2", "b"]


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
        time_budget_s=1.2,
    )
    result = await _service().debate(request)
    assert result.stop_reason == "budget" and len(result.rounds) == 1  # cut at the round-1 deadline
    by_cli = {c.target.cli: c for c in result.rounds[0].contributions}
    assert by_cli["fake"].ok and "42" in by_cli["fake"].text
    slow = by_cli["slow"]
    assert slow.ok is False and slow.error is not None and slow.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert slow.text == "" and slow.partial == "partial-so-far"  # partial kept as a trace, never the text
    assert result.rollup is not None
    assert result.rollup.stop_reason == "budget" and result.rollup.cut == 1 and result.rollup.usable == 1


async def test_debate_budget_below_quorum_raises_budget_exhausted() -> None:
    # Both voices slow: every round-1 turn is cut, leaving zero usable positions -> BUDGET_EXHAUSTED.
    request = DebateRequest(
        targets=[Target(cli="slow"), Target(cli="slow")],
        prompt="x",
        rounds=2,
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
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
        time_budget_s=1.2,
        on_budget="continue",
    )
    result = await _service().debate(request)
    assert result.stop_reason is None
    assert all(c.ok for c in result.rounds[0].contributions)  # the slow voice finished too
    assert result.rollup is not None and result.rollup.stop_reason == "ok" and result.rollup.cut == 0


async def test_debate_budget_rollup_records_effort_requested() -> None:
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        rounds=2,
        working_dir=str(REPO_ROOT),
        time_budget_s=1.2,
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
        time_budget_s=1.2,
    )
    config = RutherfordConfig(min_quorum=1)
    # both cut with no usable position -> below quorum -> BUDGET_EXHAUSTED
    with pytest.raises(RutherfordError) as exc:
        await _service(config).debate(request)
    assert exc.value.code is ErrorCode.BUDGET_EXHAUSTED
