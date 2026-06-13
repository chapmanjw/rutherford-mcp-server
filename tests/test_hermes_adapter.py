# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Hermes Agent adapter."""

from __future__ import annotations

from pathlib import Path

from rutherford.adapters.hermes import HermesAdapter
from rutherford.domain.enums import AuthState, OutputMode, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "hermes"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="hermes", model="anthropic/claude-sonnet-4.6"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {
        "target": Target(cli="hermes", model="anthropic/claude-sonnet-4.6"),
        "prompt": "say hi",
    }
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_oneshot_prompt_is_the_z_value() -> None:
    spec = HermesAdapter().build_invocation(_req(), _ctx())
    assert spec.argv[:2] == ["hermes", "-z"]
    assert spec.argv[2] == "say hi"
    assert spec.stdin is None
    assert spec.argv[spec.argv.index("-m") + 1] == "anthropic/claude-sonnet-4.6"


def test_build_invocation_folds_role_and_files_into_the_prompt() -> None:
    spec = HermesAdapter().build_invocation(_req(files=["a.py"]), _ctx(preamble="You are a reviewer."))
    assert spec.argv[2].startswith("You are a reviewer.")
    assert "- a.py" in spec.argv[2]


def test_map_safety_yolo_only_adds_a_flag() -> None:
    adapter = HermesAdapter()
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == []
    assert adapter.map_safety(SafetyMode.PROPOSE).args == []
    assert adapter.map_safety(SafetyMode.WRITE).args == []
    assert adapter.map_safety(SafetyMode.YOLO).args == ["--yolo"]


def test_capabilities_text_no_resume_write_bypass() -> None:
    caps = HermesAdapter().capabilities()
    assert caps.output_mode is OutputMode.TEXT
    assert caps.supports_resume is False
    assert caps.write_uses_bypass is True


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.txt"), duration_s=12.0)
    result = HermesAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."


def test_parse_nonzero_exit_surfaces_stderr() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr=_sample("error.txt"), duration_s=0.5)
    result = HermesAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"
    assert "inference provider" in result.error.message.lower()


def test_parse_timeout_is_timeout_error() -> None:
    result = HermesAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_empty_output_is_parse_error() -> None:
    result = HermesAdapter().parse_output(ProcessResult(exit_code=0, stdout="  \n"), _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_auth_with_credentials() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="copilot (1 credentials)"))
    assert HermesAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="no credentials"))
    assert HermesAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN
