# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Kilo Code adapter."""

from __future__ import annotations

from pathlib import Path

from rutherford.adapters.kilo import KiloAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "kilo"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="kilo", model="kilo/anthropic/claude-opus-4.8"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {
        "target": Target(cli="kilo", model="kilo/anthropic/claude-opus-4.8"),
        "prompt": "say hi",
    }
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_shape_prompt_is_trailing_positional() -> None:
    spec = KiloAdapter().build_invocation(_req(working_dir="/work", session_id="ses-1"), _ctx())
    assert spec.argv[:4] == ["kilo", "run", "--format", "json"]
    assert spec.argv[-1] == "say hi"
    assert spec.stdin is None
    assert spec.argv[spec.argv.index("-m") + 1] == "kilo/anthropic/claude-opus-4.8"
    assert spec.argv[spec.argv.index("--dir") + 1] == "/work"
    assert spec.argv[spec.argv.index("-s") + 1] == "ses-1"


def test_build_invocation_folds_role_into_the_prompt() -> None:
    spec = KiloAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert spec.argv[-1].startswith("You are a reviewer.")


def test_map_safety_postures() -> None:
    adapter = KiloAdapter()
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == []
    assert adapter.map_safety(SafetyMode.PROPOSE).args == []
    assert adapter.map_safety(SafetyMode.WRITE).args == ["--auto"]
    assert adapter.map_safety(SafetyMode.YOLO).args == ["--dangerously-skip-permissions"]


def test_map_safety_fails_closed() -> None:
    class _FutureMode:
        value = "audit"

    assert KiloAdapter().map_safety(_FutureMode()).args == []  # type: ignore[arg-type]


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=37.0)
    result = KiloAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "ses_13d1c110cffe148YYuAgogX4th"
    assert result.cost is not None
    assert result.cost.input_tokens == 15263
    assert result.cost.output_tokens == 3
    assert result.cost.usd == 0.0123


def test_parse_error_event_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout=_sample("error.jsonl"), stderr="", duration_s=1.0)
    result = KiloAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "rate limit" in result.error.message.lower()


def test_parse_timeout_is_timeout_error() -> None:
    result = KiloAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_no_text_is_parse_error() -> None:
    raw = ProcessResult(
        exit_code=0, stdout='{"type":"step_start","sessionID":"s","part":{"type":"step-start"}}', duration_s=0.1
    )
    result = KiloAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert KiloAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"type":"text"}')) is True
    assert KiloAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="plain")) is False


def test_available_models_lists_via_probe() -> None:
    probe = FakeProbe(
        run_fn=lambda argv: ProcessResult(
            exit_code=0, stdout="kilo/anthropic/claude-opus-4.8\nkilo/openai/gpt-latest\n\nbanner line"
        )
    )
    assert KiloAdapter(probe=probe).available_models() == ["kilo/anthropic/claude-opus-4.8", "kilo/openai/gpt-latest"]


def test_available_models_falls_back_on_failure() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="boom"))
    assert KiloAdapter(probe=probe).available_models() == []


def test_check_auth_with_providers() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="anthropic (1 credential)"))
    assert KiloAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="no providers"))
    assert KiloAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN
