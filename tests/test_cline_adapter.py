# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Cline adapter."""

from __future__ import annotations

from pathlib import Path

from rutherford.adapters.cline import ClineAdapter
from rutherford.domain.enums import AuthState, Effort, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "cline"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(
    *,
    safety: SafetyMode = SafetyMode.READ_ONLY,
    preamble: str | None = None,
    effort: Effort | None = None,
    extra_args: list[str] | None = None,
) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="cline", model="gpt-5.4"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
        effort=effort,
        extra_args=extra_args or [],
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="cline", model="gpt-5.4"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_shape_prompt_is_trailing_positional() -> None:
    spec = ClineAdapter().build_invocation(_req(), _ctx())
    assert spec.argv[:2] == ["cline", "--json"]
    assert spec.argv[-1] == "say hi"  # the prompt is the trailing positional, not on stdin
    assert spec.stdin is None
    assert "--plan" in spec.argv  # read-only is always explicit
    assert "-m" in spec.argv and spec.argv[spec.argv.index("-m") + 1] == "gpt-5.4"


def test_build_invocation_includes_dir_role_effort_but_not_resume() -> None:
    spec = ClineAdapter().build_invocation(
        _req(working_dir="/work", session_id="conv-1"), _ctx(preamble="You are a reviewer.", effort=Effort.HIGH)
    )
    assert spec.argv[spec.argv.index("-c") + 1] == "/work"
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("-s") + 1] == "You are a reviewer."
    assert spec.argv[spec.argv.index("--thinking") + 1] == "high"
    # cline's --id resume rejects a headless follow-up prompt, so a session_id is never wired in.
    assert "--id" not in spec.argv
    assert not ClineAdapter().capabilities().supports_resume


def test_build_invocation_extra_args_precede_the_positional_prompt() -> None:
    spec = ClineAdapter().build_invocation(_req(), _ctx(extra_args=["-P", "anthropic"]))
    assert spec.argv[-3:] == ["-P", "anthropic", "say hi"]


def test_map_safety_plan_for_readonly_auto_approve_for_write() -> None:
    adapter = ClineAdapter()
    # Plan mode ALONE still applies an edit (verified live); read-only also denies auto-approval.
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == ["--plan", "--auto-approve", "false"]
    assert adapter.map_safety(SafetyMode.PROPOSE).args == ["--plan", "--auto-approve", "false"]
    assert adapter.map_safety(SafetyMode.WRITE).args == ["--auto-approve", "true"]
    assert adapter.map_safety(SafetyMode.YOLO).args == ["--auto-approve", "true"]


def test_map_safety_fails_closed() -> None:
    class _FutureMode:
        value = "audit"

    assert ClineAdapter().map_safety(_FutureMode()).args == ["--plan", "--auto-approve", "false"]  # type: ignore[arg-type]


def test_map_effort_supports_every_tier_including_xhigh() -> None:
    for tier in Effort:
        flags = ClineAdapter().map_effort(tier)
        assert flags.args == ["--thinking", tier.value]
        assert flags.applied is tier


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=7.0)
    result = ClineAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "conv_1781386093304_t1s21tl"
    assert result.cost is not None
    assert result.cost.input_tokens == 5751
    assert result.cost.output_tokens == 122
    assert result.cost.usd == 0.0104475


def test_parse_error_event_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout=_sample("error.jsonl"), stderr="", duration_s=0.1)
    result = ClineAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "requires a prompt" in result.error.message.lower()


def test_parse_timeout_is_timeout_error() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, duration_s=5.0)
    result = ClineAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_no_run_result_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"type":"hook_event","taskId":"t1"}', duration_s=0.1)
    result = ClineAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert ClineAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"type":"run_result"}')) is True
    assert ClineAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="plain")) is False


def test_check_auth_is_unknown() -> None:
    assert ClineAdapter(probe=FakeProbe()).check_auth().state is AuthState.UNKNOWN


def test_available_models_static_is_empty() -> None:
    assert ClineAdapter().available_models() == []
