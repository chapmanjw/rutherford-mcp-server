# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Kimi Code adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.kimi import KimiAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "kimi"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="kimi", model="k2"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="kimi", model="k2"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_shape() -> None:
    spec = KimiAdapter().build_invocation(_req(session_id="session-1"), _ctx())
    assert spec.argv[:2] == ["kimi", "-p"]
    assert spec.argv[2] == "say hi"
    assert spec.argv[3:5] == ["--output-format", "stream-json"]
    assert spec.stdin is None
    assert spec.argv[spec.argv.index("-m") + 1] == "k2"
    assert spec.argv[spec.argv.index("-S") + 1] == "session-1"


def test_build_invocation_folds_role_into_the_prompt() -> None:
    spec = KimiAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert spec.argv[2].startswith("You are a reviewer.")


def test_map_safety_is_one_fixed_posture() -> None:
    adapter = KimiAdapter()
    for mode in SafetyMode:
        assert adapter.map_safety(mode).args == []


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=4.0)
    result = KimiAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "session_4a717c1d-ac61-4614-ae96-6b8e04dad1d5"


def test_parse_error_role_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error.jsonl"), duration_s=1.0)
    result = KimiAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "provider unavailable" in result.error.message.lower()


def test_parse_nonzero_exit_is_failure() -> None:
    raw = ProcessResult(exit_code=2, stdout="", stderr="error: Cannot combine --prompt with --plan.", duration_s=0.1)
    result = KimiAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"


def test_parse_timeout_is_timeout_error() -> None:
    result = KimiAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_no_assistant_is_parse_error() -> None:
    raw = ProcessResult(
        exit_code=0, stdout='{"role":"meta","type":"session.resume_hint","session_id":"s"}', duration_s=0.1
    )
    result = KimiAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert KimiAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"role":"assistant"}')) is True
    assert KimiAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="plain")) is False


def test_check_auth_with_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "k")
    assert KimiAdapter(probe=FakeProbe()).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_with_provider_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="moonshot (3 models)"))
    assert KimiAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="no providers"))
    assert KimiAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN
