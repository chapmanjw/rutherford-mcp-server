# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Amp adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.amp import AmpAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "amp"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(
    *,
    safety: SafetyMode = SafetyMode.READ_ONLY,
    preamble: str | None = None,
    extra_args: list[str] | None = None,
) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="amp", model=None),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
        extra_args=extra_args or [],
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="amp", model=None), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = AmpAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:2] == ["amp", "-x"]
    assert spec.argv[2] == "say hi"
    assert "--stream-json" in spec.argv
    assert spec.stdin is None


def test_build_invocation_folds_role_and_files_into_the_prompt_arg() -> None:
    spec = AmpAdapter().build_invocation(_req(files=["a.py"]), _ctx(preamble="You are a reviewer."))
    prompt = spec.argv[2]
    assert prompt.startswith("You are a reviewer.")
    assert "Files in scope:" in prompt and "- a.py" in prompt
    # Amp has no system-prompt flag; the preamble is part of the prompt value, not a separate arg.
    assert spec.argv.count("You are a reviewer.") == 0


def test_build_invocation_sets_cwd_and_appends_extra_args() -> None:
    spec = AmpAdapter().build_invocation(_req(working_dir="/work"), _ctx(extra_args=["--mode", "deep"]))
    assert spec.cwd == "/work"
    assert spec.argv[-2:] == ["--mode", "deep"]


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = AmpAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    assert spec.argv[2] == "rm -rf / ; echo pwned"
    assert all(arg == "rm -rf / ; echo pwned" or ";" not in arg for arg in spec.argv)


def test_map_safety_is_best_effort_for_every_mode() -> None:
    adapter = AmpAdapter()
    for mode in SafetyMode:
        assert adapter.map_safety(mode).args == []


def test_capabilities_flag_write_uses_bypass() -> None:
    caps = AmpAdapter().capabilities()
    assert caps.write_uses_bypass is True
    assert caps.supports_model_selection is False


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=2.3)
    result = AmpAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "T-019ec2e1-e12b-7198-9944-fee3b9644d25"
    assert result.cost is not None
    assert result.cost.input_tokens == 12
    assert result.cost.output_tokens == 8


def test_parse_in_band_error_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error.jsonl"), duration_s=1.2)
    result = AmpAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "could not complete" in result.error.message.lower()


def test_parse_nonzero_exit_is_failure() -> None:
    raw = ProcessResult(exit_code=2, stdout="", stderr="error: not authenticated", duration_s=0.4)
    result = AmpAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"


def test_parse_timeout_is_timeout_error() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, partial="half", duration_s=5.0)
    result = AmpAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "TIMEOUT"
    assert result.partial == "half"


def test_parse_no_result_event_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"type":"system","subtype":"init"}', duration_s=0.1)
    result = AmpAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert AmpAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"type":"result"}')) is True
    assert AmpAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="plain text")) is False


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMP_API_KEY", "amp-test")
    assert AmpAdapter(probe=FakeProbe()).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_with_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="Signed in as a@b.c"))
    assert AmpAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMP_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="not logged in"))
    assert AmpAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN


def test_provenance_reports_anthropic() -> None:
    prov = AmpAdapter().provenance(_ctx())
    assert prov.provider == "anthropic"
