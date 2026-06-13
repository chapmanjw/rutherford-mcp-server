# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the F8a effort knob: ``map_effort`` per adapter, clamping, and end-to-end flow.

The universal :class:`Effort` tier maps per adapter to that CLI's native knob (``map_effort``),
clamps to the nearest supported tier, no-ops + reports where unsupported, and is reported as the
``effort_applied`` actually enforced -- so a budget that silently did nothing is never silent.
"""

from __future__ import annotations

import pytest

from rutherford.adapters.base import BaseCLIAdapter
from rutherford.adapters.claude_code import ClaudeCodeAdapter
from rutherford.adapters.codex import CodexAdapter
from rutherford.adapters.cursor import CursorAdapter
from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import AdapterConfig, RutherfordConfig
from rutherford.domain.enums import EFFORT_ORDER, Effort
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from rutherford.runtime.platform import OSFamily, PlatformInfo
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


def _codex_ctx(effort: Effort | None) -> InvocationContext:
    return InvocationContext(target=Target(cli="codex", model="gpt-5-codex"), correlation_id="t", effort=effort)


def _codex_req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="codex", model="gpt-5-codex"), "prompt": "hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def _linux_codex() -> CodexAdapter:
    # A fixed non-Windows platform so the Windows-only sandbox override does not appear in the argv here.
    return CodexAdapter(platform=PlatformInfo(os_family=OSFamily.LINUX, is_wsl=False))


# --- the base no-op default ---------------------------------------------------------------------


def test_base_map_effort_is_a_reported_no_op() -> None:
    # claude_code does not override map_effort, so it exercises the base no-op default: no flag added,
    # applied=None (the "ignored" marker), and a note that names the adapter -- never a silent drop.
    flags = ClaudeCodeAdapter().map_effort(Effort.HIGH)
    assert flags.args == []  # no flag added
    assert flags.applied is None  # the no-op marker
    assert "not supported" in flags.note and "claude_code" in flags.note  # reported, never silent


@pytest.mark.parametrize(
    ("effort", "ceiling", "expected"),
    [
        (Effort.LOW, Effort.HIGH, Effort.LOW),
        (Effort.HIGH, Effort.HIGH, Effort.HIGH),
        (Effort.XHIGH, Effort.HIGH, Effort.HIGH),  # clamps down to the ceiling
        (Effort.XHIGH, Effort.MEDIUM, Effort.MEDIUM),
        (Effort.LOW, Effort.LOW, Effort.LOW),
    ],
)
def test_clamp_effort(effort: Effort, ceiling: Effort, expected: Effort) -> None:
    assert BaseCLIAdapter._clamp_effort(effort, ceiling) is expected


def test_effort_order_is_least_to_most() -> None:
    assert EFFORT_ORDER == (Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH)


# --- codex: -c model_reasoning_effort=<tier> ----------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "applied"),
    [(Effort.LOW, "low"), (Effort.MEDIUM, "medium"), (Effort.HIGH, "high"), (Effort.XHIGH, "high")],
)
def test_codex_map_effort_maps_and_clamps(requested: Effort, applied: str) -> None:
    flags = CodexAdapter().map_effort(requested)
    assert flags.args == ["-c", f"model_reasoning_effort={applied}"]
    assert flags.applied is Effort(applied)


def test_codex_map_effort_notes_the_clamp() -> None:
    flags = CodexAdapter().map_effort(Effort.XHIGH)
    assert "clamped from xhigh" in flags.note


def test_codex_fresh_invocation_carries_the_effort_override() -> None:
    spec = _linux_codex().build_invocation(_codex_req(), _codex_ctx(Effort.HIGH))
    assert "-c" in spec.argv
    assert "model_reasoning_effort=high" in spec.argv


def test_codex_no_effort_adds_no_override() -> None:
    spec = _linux_codex().build_invocation(_codex_req(), _codex_ctx(None))
    assert not any("model_reasoning_effort" in arg for arg in spec.argv)


def test_codex_resume_puts_effort_before_the_positional_separator() -> None:
    # The resume path uses a ``--`` positional separator; a ``-c`` override appended AFTER it would be
    # parsed as a positional, not a flag. The effort args must land before ``--``.
    spec = _linux_codex().build_invocation(_codex_req(session_id="sess-1"), _codex_ctx(Effort.HIGH))
    sep = spec.argv.index("--")
    effort_value = spec.argv.index("model_reasoning_effort=high")
    assert effort_value < sep  # the override precedes the session/prompt positionals
    assert spec.argv[sep + 1 :] == ["sess-1", "-"]


# --- cursor: the effort-in-model-id convention --------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "applied"),
    [(Effort.LOW, Effort.LOW), (Effort.HIGH, Effort.HIGH), (Effort.XHIGH, Effort.HIGH)],
)
def test_cursor_map_effort_reports_applied(requested: Effort, applied: Effort) -> None:
    flags = CursorAdapter().map_effort(requested)
    assert flags.args == []  # cursor has no free-standing flag; the tier rides the model id
    assert flags.applied is applied


def test_cursor_rewrites_the_model_id_with_a_tier_suffix() -> None:
    # Cursor's effort convention is a plain ``-<tier>`` suffix (gpt-5.2-high, claude-opus-4-8-high),
    # confirmed against ``cursor-agent --list-models`` -- NOT ``-thinking-`` (a Claude-only axis that
    # would produce an invalid id for the gpt families).
    spec = CursorAdapter().build_invocation(
        DelegationRequest(target=Target(cli="cursor", model="gpt-5.2"), prompt="hi"),
        InvocationContext(target=Target(cli="cursor", model="gpt-5.2"), correlation_id="t", effort=Effort.HIGH),
    )
    assert "gpt-5.2-high" in spec.argv
    assert "gpt-5.2-thinking-high" not in spec.argv


def test_cursor_xhigh_clamps_in_the_model_id() -> None:
    assert CursorAdapter._model_with_effort("gpt-5.2", Effort.XHIGH) == "gpt-5.2-high"


def test_cursor_leaves_auto_and_already_tiered_models_unchanged() -> None:
    # ``auto`` (the universal fallback) has no tiered variant; a model that already encodes an effort
    # (a trailing tier, a -fast variant, or a thinking segment) must not be double-suffixed into an id
    # Cursor would reject -- the user's explicit choice is respected.
    assert CursorAdapter._model_with_effort("auto", Effort.HIGH) == "auto"
    assert CursorAdapter._model_with_effort("gpt-5.2-high", Effort.LOW) == "gpt-5.2-high"
    assert CursorAdapter._model_with_effort("gpt-5.2-high-fast", Effort.LOW) == "gpt-5.2-high-fast"
    thinking = "claude-opus-4-8-thinking-low"
    assert CursorAdapter._model_with_effort(thinking, Effort.HIGH) == thinking  # thinking axis untouched
    assert CursorAdapter._model_with_effort("claude-opus-4-8", Effort.MEDIUM) == "claude-opus-4-8-medium"


def test_cursor_does_not_append_a_tier_after_a_fast_serving_variant() -> None:
    # A ``-fast`` model already names its serving choice (and its tier, or has none): appending a tier
    # *after* ``-fast`` would invent an invalid id like ``composer-2.5-fast-high``. Leave it unchanged.
    assert CursorAdapter._model_with_effort("composer-2.5-fast", Effort.HIGH) == "composer-2.5-fast"
    assert CursorAdapter._model_with_effort("gpt-5.2-fast", Effort.HIGH) == "gpt-5.2-fast"


# --- end-to-end through the delegation service --------------------------------------------------


def _delegation(adapter: FakeAdapter, runner: FakeProcessRunner, config: RutherfordConfig | None = None):
    cfg = config or RutherfordConfig()
    return DelegationService(AdapterRegistry([adapter]), runner, cfg, load_roles())


async def test_request_effort_flows_to_the_invocation_and_is_reported_applied() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _delegation(FakeAdapter("a"), runner)
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q", effort=Effort.MEDIUM))
    assert result.effort is Effort.MEDIUM
    assert result.effort_applied is Effort.MEDIUM  # the fake echoes the tier as applied
    spec, _ = runner.calls[0]
    assert "--effort=medium" in spec.argv  # the resolved effort reached build_invocation


async def test_config_default_effort_fills_when_the_call_omits_it() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _delegation(FakeAdapter("a"), runner, RutherfordConfig(default_effort=Effort.LOW))
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q"))  # no effort named
    assert result.effort is Effort.LOW
    spec, _ = runner.calls[0]
    assert "--effort=low" in spec.argv


async def test_explicit_call_effort_wins_over_the_config_default() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _delegation(FakeAdapter("a"), runner, RutherfordConfig(default_effort=Effort.LOW))
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q", effort=Effort.XHIGH))
    assert result.effort is Effort.XHIGH


async def test_no_effort_anywhere_leaves_the_result_effort_unset() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _delegation(FakeAdapter("a"), runner)  # no default_effort, no call effort
    result = await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q"))
    assert result.effort is None
    assert result.effort_applied is None
    spec, _ = runner.calls[0]
    assert not any(arg.startswith("--effort=") for arg in spec.argv)


# --- config resolution: effort_for (per-adapter, else global) -----------------------------------


def test_effort_for_prefers_the_per_adapter_value() -> None:
    config = RutherfordConfig(
        default_effort=Effort.LOW,
        adapters={"codex": AdapterConfig(effort=Effort.XHIGH)},
    )
    assert config.effort_for("codex") is Effort.XHIGH  # per-adapter wins
    assert config.effort_for("claude_code") is Effort.LOW  # falls back to the global default


def test_effort_for_is_none_when_unset() -> None:
    assert RutherfordConfig().effort_for("codex") is None  # let the CLI decide; no flag
