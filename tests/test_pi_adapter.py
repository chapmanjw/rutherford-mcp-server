# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the pi adapter."""

from __future__ import annotations

from pathlib import Path

from rutherford.adapters.pi import PiAdapter
from rutherford.domain.enums import AuthState, Effort, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "pi"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(
    *, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None, effort: Effort | None = None
) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="pi", model="moonshotai/Kimi-K2.6"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
        effort=effort,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="pi", model="moonshotai/Kimi-K2.6"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_shape_prompt_is_trailing_positional() -> None:
    spec = PiAdapter().build_invocation(_req(), _ctx())
    assert spec.argv[:4] == ["pi", "-p", "--mode", "json"]
    assert spec.argv[-1] == "say hi"
    assert spec.stdin is None
    assert spec.argv[spec.argv.index("--model") + 1] == "moonshotai/Kimi-K2.6"


def test_build_invocation_readonly_restricts_the_toolset() -> None:
    spec = PiAdapter().build_invocation(_req(), _ctx())
    assert spec.argv[spec.argv.index("--tools") + 1] == "read,grep,find,ls"


def test_build_invocation_role_effort_session() -> None:
    spec = PiAdapter().build_invocation(
        _req(session_id="019-abc"), _ctx(preamble="You are a reviewer.", effort=Effort.XHIGH)
    )
    assert spec.argv[spec.argv.index("--system-prompt") + 1] == "You are a reviewer."
    assert spec.argv[spec.argv.index("--thinking") + 1] == "xhigh"
    assert spec.argv[spec.argv.index("--session-id") + 1] == "019-abc"


def test_map_safety_readonly_restricts_write_does_not() -> None:
    adapter = PiAdapter()
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == ["--tools", "read,grep,find,ls"]
    assert adapter.map_safety(SafetyMode.PROPOSE).args == ["--tools", "read,grep,find,ls"]
    assert adapter.map_safety(SafetyMode.WRITE).args == []
    assert adapter.map_safety(SafetyMode.YOLO).args == []


def test_map_safety_fails_closed() -> None:
    class _FutureMode:
        value = "audit"

    assert PiAdapter().map_safety(_FutureMode()).args == ["--tools", "read,grep,find,ls"]  # type: ignore[arg-type]


def test_map_effort_supports_every_tier() -> None:
    for tier in Effort:
        flags = PiAdapter().map_effort(tier)
        assert flags.args == ["--thinking", tier.value]
        assert flags.applied is tier


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=3.0)
    result = PiAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "019ec2e3-3d86-76e7-8991-03662beab98f"
    assert result.cost is not None
    assert result.cost.usd == 0.00099853
    assert result.cost.input_tokens == 883
    assert result.cost.output_tokens == 34


def test_parse_error_event_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout=_sample("error.jsonl"), stderr="", duration_s=0.5)
    result = PiAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "model unavailable" in result.error.message.lower()


def test_parse_timeout_is_timeout_error() -> None:
    result = PiAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_no_assistant_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"type":"session","id":"s"}\n{"type":"agent_start"}', duration_s=0.1)
    result = PiAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert PiAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"type":"message_end"}')) is True
    assert PiAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="plain")) is False


def test_available_models_parses_the_table() -> None:
    table = (
        "provider     model                         context  max-out  thinking  images\n"
        "huggingface  moonshotai/Kimi-K2.6          262.1K   16.4K    yes       no\n"
        "huggingface  deepseek-ai/DeepSeek-V3.2     163.8K   65.5K    yes       no\n"
    )
    probe = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout=table))
    assert PiAdapter(probe=probe).available_models() == ["moonshotai/Kimi-K2.6", "deepseek-ai/DeepSeek-V3.2"]


def test_available_models_falls_back_on_failure() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="boom"))
    assert PiAdapter(probe=probe).available_models() == []


def test_check_auth_with_configured_provider() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="provider model ...\nhuggingface x"))
    assert PiAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="no provider configured"))
    assert PiAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN
