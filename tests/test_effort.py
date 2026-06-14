# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for reasoning-effort tiers (F8a): the per-agent ACP override mapping and end-to-end resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.effort import EffortOverride, effort_overrides
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.enums import Effort
from rutherford.domain.models import DelegationRequest, Target
from rutherford.services.delegation import DelegationService

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))


def _descriptor(agent_id: str, *, model: str | None = None) -> AgentDescriptor:
    return AgentDescriptor(agent_id, agent_id.title(), _FAKE_CMD, default_model=model)


# --- the per-agent override mapping ------------------------------------------


def test_codex_encodes_effort_in_the_model_id() -> None:
    override = effort_overrides(_descriptor("codex"), Effort.HIGH, model="gpt-5.2")
    assert override.model == "gpt-5.2[high]"
    assert override.applied is Effort.HIGH
    assert override.extra_args == () and override.extra_env == ()


def test_codex_supports_xhigh_without_clamping() -> None:
    override = effort_overrides(_descriptor("codex"), Effort.XHIGH, model="gpt-5.2")
    assert override.model == "gpt-5.2[xhigh]" and override.applied is Effort.XHIGH


def test_codex_replaces_an_existing_bracket() -> None:
    override = effort_overrides(_descriptor("codex"), Effort.LOW, model="gpt-5.2[high]")
    assert override.model == "gpt-5.2[low]"


def test_codex_without_a_model_is_a_reported_noop() -> None:
    override = effort_overrides(_descriptor("codex"), Effort.HIGH, model=None)
    assert override.model is None and override.applied is None
    assert "none resolved" in override.note


def test_cursor_appends_a_model_suffix_and_clamps_xhigh() -> None:
    high = effort_overrides(_descriptor("cursor"), Effort.HIGH, model="gpt-5.2")
    assert high.model == "gpt-5.2-high" and high.applied is Effort.HIGH
    xhigh = effort_overrides(_descriptor("cursor"), Effort.XHIGH, model="gpt-5.2")
    assert xhigh.model == "gpt-5.2-high" and xhigh.applied is Effort.HIGH  # cursor tops out at high
    assert "clamped from xhigh" in xhigh.note


def test_cursor_leaves_auto_and_already_tiered_models_unchanged() -> None:
    auto = effort_overrides(_descriptor("cursor"), Effort.HIGH, model="auto")
    assert auto.model is None and auto.applied is Effort.HIGH  # nothing rewritten, but the tier is reported
    tiered = effort_overrides(_descriptor("cursor"), Effort.HIGH, model="gpt-5.2-medium")
    assert tiered.model is None and "already carries" in tiered.note


def test_cline_uses_the_thinking_launch_flag_for_every_tier() -> None:
    override = effort_overrides(_descriptor("cline"), Effort.XHIGH, model=None)
    assert override.extra_args == ("--thinking", "xhigh") and override.applied is Effort.XHIGH
    assert override.model is None and override.extra_env == ()


def test_junie_sets_the_effort_env_best_effort() -> None:
    override = effort_overrides(_descriptor("junie"), Effort.MEDIUM, model=None)
    assert override.extra_env == (("JUNIE_EFFORT", "medium"),) and override.applied is Effort.MEDIUM
    assert "best-effort" in override.note


def test_pi_is_an_honest_noop() -> None:
    # pi's --thinking is an in-session RPC selector, not a launch knob -- so it is a reported no-op, never a flag.
    override = effort_overrides(_descriptor("pi"), Effort.HIGH, model="some-model")
    assert override == EffortOverride(note="effort 'high' is not supported by pi; ignored")
    assert override.applied is None and override.extra_args == () and override.model is None


def test_unknown_agent_is_a_reported_noop() -> None:
    override = effort_overrides(_descriptor("goose"), Effort.HIGH, model="m")
    assert override.applied is None and "not supported by goose" in override.note


def test_none_effort_is_a_clean_noop() -> None:
    override = effort_overrides(_descriptor("codex"), None, model="gpt-5.2")
    assert override == EffortOverride() and override.applied is None and override.note == ""


# --- effort resolution precedence (call > per-agent > default > none) --------


def _delegation(config: RutherfordConfig, descriptor: AgentDescriptor) -> DelegationService:
    return DelegationService(DescriptorRegistry([descriptor]), config)


def test_resolve_effort_call_value_wins() -> None:
    config = RutherfordConfig(default_effort=Effort.LOW, agents={"cline": AgentConfig(effort=Effort.MEDIUM)})
    service = _delegation(config, _descriptor("cline"))
    assert service.resolve_effort("cline", Effort.HIGH) is Effort.HIGH  # explicit call beats both configs


def test_resolve_effort_per_agent_beats_global_default() -> None:
    config = RutherfordConfig(default_effort=Effort.LOW, agents={"cline": AgentConfig(effort=Effort.MEDIUM)})
    service = _delegation(config, _descriptor("cline"))
    assert service.resolve_effort("cline", None) is Effort.MEDIUM


def test_resolve_effort_falls_back_to_global_default() -> None:
    config = RutherfordConfig(default_effort=Effort.LOW)
    service = _delegation(config, _descriptor("cline"))
    assert service.resolve_effort("cline", None) is Effort.LOW


def test_resolve_effort_is_none_when_nothing_configured() -> None:
    service = _delegation(RutherfordConfig(), _descriptor("cline"))
    assert service.resolve_effort("cline", None) is None  # let the agent decide


# --- effort_applied on the delegation result (end to end over the fake agent) -


async def test_delegate_stamps_effort_and_applied_for_an_agent_with_a_knob() -> None:
    # 'cline' has a real --thinking knob, so a high effort run echoes effort=high + effort_applied=high.
    service = _delegation(RutherfordConfig(), _descriptor("cline"))
    request = DelegationRequest(
        target=Target(cli="cline"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), effort=Effort.HIGH
    )
    result = await service.delegate(request)
    assert result.ok and "42" in result.text
    assert result.effort is Effort.HIGH and result.effort_applied is Effort.HIGH


async def test_delegate_reports_noop_effort_for_an_agent_without_a_knob() -> None:
    # 'goose' has no knob: the request still records the requested effort, but effort_applied stays None.
    service = _delegation(RutherfordConfig(), _descriptor("goose"))
    request = DelegationRequest(
        target=Target(cli="goose"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), effort=Effort.HIGH
    )
    result = await service.delegate(request)
    assert result.ok and result.effort is Effort.HIGH and result.effort_applied is None


async def test_delegate_default_effort_takes_effect_when_call_omits_it() -> None:
    config = RutherfordConfig(default_effort=Effort.MEDIUM)
    service = _delegation(config, _descriptor("cline"))
    request = DelegationRequest(target=Target(cli="cline"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    result = await service.delegate(request)
    assert result.effort is Effort.MEDIUM and result.effort_applied is Effort.MEDIUM


async def test_codex_effort_selects_the_rewritten_model_over_acp(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake advertises the effort-rewritten id, so the client's best-effort set_model selects it; the
    # session's resolved target carries 'gpt-5.2[high]', confirming the model-id encoding reaches the agent.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "gpt-5.2[high],gpt-5.2")
    service = _delegation(RutherfordConfig(), _descriptor("codex", model="gpt-5.2"))
    request = DelegationRequest(
        target=Target(cli="codex"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), effort=Effort.HIGH
    )
    result = await service.delegate(request)
    assert result.ok and result.effort is Effort.HIGH and result.effort_applied is Effort.HIGH
    assert result.target.model == "gpt-5.2[high]"
