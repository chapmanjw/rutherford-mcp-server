# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for reasoning-effort tiers (F8a): the per-agent ACP override mapping and end-to-end resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.effort import EffortOverride, clamp_to_supported, effort_overrides
from rutherford.acp.roster import build_registry
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


def test_codex_without_a_model_routes_to_the_config_option() -> None:
    # No model to bracket: codex-acp also exposes a 'reasoning_effort' config option, so the no-model case
    # routes to the config-option channel (applied is resolved at session open, not here) instead of dropping.
    override = effort_overrides(_descriptor("codex"), Effort.HIGH, model=None)
    assert override.via_config_option and override.model is None and override.applied is None
    assert override.extra_args == () and override.extra_env == ()


def test_codex_max_clamps_to_xhigh_on_the_model_id() -> None:
    # Codex tops out at xhigh, so a 'max' request on the model-id channel clamps down and says so.
    override = effort_overrides(_descriptor("codex"), Effort.MAX, model="gpt-5.5")
    assert override.model == "gpt-5.5[xhigh]" and override.applied is Effort.XHIGH
    assert "clamped from max" in override.note


def test_claude_code_routes_to_the_config_option() -> None:
    # claude-agent-acp carries effort via its 'effort' config option (not a launch flag / model id), so the
    # builder just routes there; the applied tier is resolved at session open against the model's levels.
    override = effort_overrides(_descriptor("claude_code"), Effort.XHIGH, model="opus")
    assert override.via_config_option and override.applied is None
    assert override.extra_args == () and override.extra_env == () and override.model is None


def test_kiro_uses_the_effort_launch_flag_for_every_tier() -> None:
    for tier in Effort:
        override = effort_overrides(_descriptor("kiro"), tier, model=None)
        assert override.extra_args == ("--effort", tier.value) and override.applied is tier
        assert not override.via_config_option and override.extra_env == () and override.model is None


def test_clamp_to_supported_picks_the_request_or_the_nearest_below() -> None:
    codex = [Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH]
    assert clamp_to_supported(Effort.HIGH, codex) is Effort.HIGH  # offered -> exact
    assert clamp_to_supported(Effort.MAX, codex) is Effort.XHIGH  # above the ceiling -> highest below
    assert clamp_to_supported(Effort.LOW, [Effort.HIGH, Effort.XHIGH]) is Effort.HIGH  # below all -> lowest
    assert clamp_to_supported(Effort.HIGH, []) is None  # nothing advertised -> no-op


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


# --- base= clones inherit the base adapter's effort knob (lineage-then-id dispatch) -----------------------


def _clone(agent_id: str, base: str) -> AgentDescriptor:
    """A config clone of a built-in, built through the REAL roster so ``effort_base`` is stamped."""
    config = RutherfordConfig(auto_detect_local_models=False, agents={agent_id: AgentConfig(base=base)})
    return build_registry(config).get(agent_id)


def test_base_clone_of_codex_with_a_model_inherits_the_model_id_bracket() -> None:
    # The whole bug in one test: before the lineage fix this was the not-supported no-op; now the clone runs
    # the real _codex builder and encodes the tier in the model id, exactly like base codex.
    override = effort_overrides(_clone("my-codex", "codex"), Effort.HIGH, model="gpt-5.2")
    assert override.model == "gpt-5.2[high]" and override.applied is Effort.HIGH


def test_base_clone_of_codex_with_no_model_routes_to_the_config_option() -> None:
    override = effort_overrides(_clone("my-codex", "codex"), Effort.HIGH, model=None)
    assert override.via_config_option and override.model is None and override.applied is None


def test_base_clone_of_claude_code_routes_to_the_config_option() -> None:
    override = effort_overrides(_clone("claude-sonnet", "claude_code"), Effort.XHIGH, model="opus")
    assert override.via_config_option and override.applied is None and override.model is None


def test_base_clone_of_cline_inherits_the_thinking_flag() -> None:
    override = effort_overrides(_clone("my-cline", "cline"), Effort.HIGH, model=None)
    assert override.extra_args == ("--thinking", "high") and override.applied is Effort.HIGH


def test_base_clone_of_kiro_inherits_the_effort_flag() -> None:
    override = effort_overrides(_clone("my-kiro", "kiro"), Effort.HIGH, model=None)
    assert override.extra_args == ("--effort", "high") and override.applied is Effort.HIGH


def test_base_clone_of_junie_inherits_the_junie_effort_env() -> None:
    override = effort_overrides(_clone("my-junie", "junie"), Effort.MEDIUM, model=None)
    assert ("JUNIE_EFFORT", "medium") in override.extra_env and override.applied is Effort.MEDIUM


def test_base_clone_of_cursor_with_a_model_inherits_the_suffix_and_clamp() -> None:
    override = effort_overrides(_clone("my-cursor", "cursor"), Effort.MAX, model="gpt-5.2")
    assert override.model == "gpt-5.2-high" and override.applied is Effort.HIGH  # cursor tops out at high


def test_base_clone_of_cursor_with_no_model_is_an_honest_noop() -> None:
    # Parity with base cursor's no-model branch: nothing to rewrite, so an honest no-op -- NOT a forced tier.
    override = effort_overrides(_clone("my-cursor", "cursor"), Effort.HIGH, model=None)
    assert override.applied is None


def test_base_clone_of_a_knobless_base_is_still_an_honest_noop() -> None:
    # goose has no effort knob; its clone inherits that honestly -- effort_base="goose" isn't in _BUILDERS.
    override = effort_overrides(_clone("my-goose", "goose"), Effort.HIGH, model="m")
    assert override.applied is None and "not supported by my-goose" in override.note


def test_a_command_only_clone_has_no_effort_lineage() -> None:
    # A raw command= agent that happens to exec an effort-capable adapter stays an honest no-op: lineage is
    # NEVER inferred from command[0] (which here is the sh wrapper, not the adapter).
    config = RutherfordConfig(
        auto_detect_local_models=False,
        agents={"raw": AgentConfig(command=["sh", "-c", "exec codex-acp"])},
    )
    descriptor = build_registry(config).get("raw")
    assert descriptor.effort_base is None
    override = effort_overrides(descriptor, Effort.HIGH, model="gpt-5.2")
    assert override.applied is None and "not supported by raw" in override.note


def test_a_same_id_override_of_codex_still_dispatches_to_codex() -> None:
    # An in-place override keeps the base id, so effort_base == id == "codex": effort still works (it always
    # did for same-id overrides, and the lineage stamp must not change that).
    config = RutherfordConfig(auto_detect_local_models=False, agents={"codex": AgentConfig(default_model="gpt-5.2")})
    descriptor = build_registry(config).get("codex")
    assert descriptor.effort_base == "codex"
    override = effort_overrides(descriptor, Effort.HIGH, model="gpt-5.2")
    assert override.model == "gpt-5.2[high]" and override.applied is Effort.HIGH


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


# --- the config-option effort channel (claude_code, codex-no-model) end to end over the fake ----------------


async def test_config_option_effort_applies_over_acp(monkeypatch: pytest.MonkeyPatch) -> None:
    # claude_code carries effort via its 'effort' config option: the fake advertises one, so Rutherford's
    # session/set_config_option sets the tier and the agent echoes it back -- proof it reached the agent.
    monkeypatch.setenv("RUTHERFORD_FAKE_EFFORT_OPTION", "effort:low,medium,high,xhigh,max")
    service = _delegation(RutherfordConfig(), _descriptor("claude_code"))
    request = DelegationRequest(
        target=Target(cli="claude_code"), prompt="EFFORT?", working_dir=str(REPO_ROOT), effort=Effort.XHIGH
    )
    result = await service.delegate(request)
    assert result.ok and "effort=xhigh" in result.text
    assert result.effort is Effort.XHIGH and result.effort_applied is Effort.XHIGH


async def test_config_option_effort_clamps_to_advertised_values(monkeypatch: pytest.MonkeyPatch) -> None:
    # codex with no model routes to the 'reasoning_effort' config option, which tops out at xhigh, so a 'max'
    # request is clamped to xhigh both in what the agent is set to AND in the reported effort_applied.
    monkeypatch.setenv("RUTHERFORD_FAKE_EFFORT_OPTION", "reasoning_effort:low,medium,high,xhigh")
    service = _delegation(RutherfordConfig(), _descriptor("codex"))
    request = DelegationRequest(
        target=Target(cli="codex"), prompt="EFFORT?", working_dir=str(REPO_ROOT), effort=Effort.MAX
    )
    result = await service.delegate(request)
    assert result.ok and "effort=xhigh" in result.text
    assert result.effort is Effort.MAX and result.effort_applied is Effort.XHIGH


async def test_config_option_effort_is_an_honest_noop_when_no_option_advertised() -> None:
    # claude_code routes to the config-option channel, but if the agent advertises NO effort option the tier is
    # a reported no-op (effort_applied None), never a silent claim it applied. (No FAKE_EFFORT_OPTION set here.)
    service = _delegation(RutherfordConfig(), _descriptor("claude_code"))
    request = DelegationRequest(
        target=Target(cli="claude_code"), prompt="EFFORT?", working_dir=str(REPO_ROOT), effort=Effort.HIGH
    )
    result = await service.delegate(request)
    assert result.ok and "effort=(unset)" in result.text
    assert result.effort is Effort.HIGH and result.effort_applied is None
