# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Continue (cn) adapter."""

from __future__ import annotations

from pathlib import Path

from rutherford.adapters.cn import ContinueAdapter
from rutherford.domain.enums import AuthState, OutputMode, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "cn"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="cn", model="anthropic/claude"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="cn", model="anthropic/claude"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_shape_prompt_is_trailing_positional() -> None:
    spec = ContinueAdapter().build_invocation(_req(), _ctx())
    assert spec.argv[:3] == ["cn", "-p", "--silent"]
    assert "--readonly" in spec.argv
    assert spec.argv[-1] == "say hi"
    assert spec.stdin is None
    assert spec.argv[spec.argv.index("--model") + 1] == "anthropic/claude"
    # Continue's --format json envelope is unreliable; the adapter reads plain text instead.
    assert "--format" not in spec.argv


def test_build_invocation_role_rides_on_rule() -> None:
    spec = ContinueAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert spec.argv[spec.argv.index("--rule") + 1] == "You are a reviewer."


def test_build_invocation_multiline_prompt_is_a_single_positional() -> None:
    # The multi-line prompt is a single argv element (PowerShell launch preserves it); never split.
    spec = ContinueAdapter().build_invocation(_req(prompt="line one\nline two"), _ctx())
    assert spec.argv[-1] == "line one\nline two"


def test_map_safety_readonly_vs_auto() -> None:
    adapter = ContinueAdapter()
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == ["--readonly"]
    assert adapter.map_safety(SafetyMode.PROPOSE).args == ["--readonly"]
    assert adapter.map_safety(SafetyMode.WRITE).args == ["--auto"]
    assert adapter.map_safety(SafetyMode.YOLO).args == ["--auto"]


def test_map_safety_fails_closed() -> None:
    class _FutureMode:
        value = "audit"

    assert ContinueAdapter().map_safety(_FutureMode()).args == ["--readonly"]  # type: ignore[arg-type]


def test_capabilities_text_no_resume() -> None:
    caps = ContinueAdapter().capabilities()
    assert caps.output_mode is OutputMode.TEXT
    assert caps.supports_resume is False
    assert caps.supports_system_prompt is True


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.txt"), duration_s=5.0)
    result = ContinueAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."


def test_parse_numeric_answer_is_kept_as_text() -> None:
    # The case that broke the old JSON parser: a numeric answer (cn used to emit {"result": 42}).
    raw = ProcessResult(exit_code=0, stdout="42\n", duration_s=1.0)
    result = ContinueAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "42"


def test_parse_nonzero_exit_surfaces_stderr() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr=_sample("error.txt"), duration_s=0.1)
    result = ContinueAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"
    assert "authenticated" in result.error.message.lower()


def test_parse_timeout_is_timeout_error() -> None:
    result = ContinueAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_empty_output_is_parse_error() -> None:
    result = ContinueAdapter().parse_output(ProcessResult(exit_code=0, stdout="  \n"), _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_auth_is_unknown() -> None:
    assert ContinueAdapter(probe=FakeProbe()).check_auth().state is AuthState.UNKNOWN
