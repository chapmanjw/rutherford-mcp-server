# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Qwen Code adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.qwen import QwenAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "qwen"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="qwen", model="qwen3-coder"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="qwen", model="qwen3-coder"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = QwenAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:3] == ["qwen", "-o", "json"]
    # The mapped approval mode is always present.
    assert "--approval-mode" in spec.argv
    assert spec.argv[spec.argv.index("--approval-mode") + 1] == "plan"
    assert "-m" in spec.argv
    assert spec.argv[spec.argv.index("-m") + 1] == "qwen3-coder"


def test_build_invocation_prompt_goes_to_stdin_not_argv() -> None:
    spec = QwenAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv


def test_build_invocation_includes_working_dir_and_resume() -> None:
    spec = QwenAdapter().build_invocation(
        _req(working_dir="/work", session_id="sess-1"),
        _ctx(),
    )
    assert "--add-dir" in spec.argv
    assert spec.argv[spec.argv.index("--add-dir") + 1] == "/work"
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("-r") + 1] == "sess-1"


def test_build_invocation_uses_system_prompt_for_role() -> None:
    spec = QwenAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert "--append-system-prompt" in spec.argv
    assert spec.argv[spec.argv.index("--append-system-prompt") + 1] == "You are a reviewer."
    # qwen has a system-prompt flag, so the preamble is never prepended to the stdin prompt.
    assert spec.stdin == "say hi"


def test_build_invocation_appends_files_to_stdin() -> None:
    spec = QwenAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    assert spec.stdin is not None
    assert "Files in scope:" in spec.stdin
    assert "- a.py" in spec.stdin


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = QwenAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text rides in stdin as a single string; it is never an argv element.
    assert spec.stdin == "rm -rf / ; echo pwned"
    assert all(";" not in arg for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = QwenAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == ["--approval-mode", "plan"]
    assert flags[SafetyMode.PROPOSE].args == ["--approval-mode", "plan"]
    assert flags[SafetyMode.WRITE].args == ["--approval-mode", "auto-edit"]
    assert flags[SafetyMode.YOLO].args == ["--approval-mode", "yolo"]


def test_build_invocation_write_mode_adds_auto_edit() -> None:
    spec = QwenAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert "auto-edit" in spec.argv


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.json"), duration_s=2.3)
    result = QwenAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "7d0fa0ac"
    assert result.cost is not None
    assert result.cost.input_tokens == 15837
    assert result.cost.output_tokens == 7


def test_parse_in_band_error_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error.json"), duration_s=1.5)
    result = QwenAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "model request failed" in result.error.message.lower()


def test_parse_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr="error: not authenticated", duration_s=0.4)
    result = QwenAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "authenticated" in result.error.message


def test_parse_timeout() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, duration_s=300.0)
    result = QwenAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not json at all", duration_s=0.1)
    result = QwenAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_falls_back_to_assistant_message() -> None:
    # An array with an assistant message but no result element: fall back to the assistant text.
    stdout = (
        '[{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"fallback answer"}]}}]'
    )
    raw = ProcessResult(exit_code=0, stdout=stdout, duration_s=0.1)
    result = QwenAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "fallback answer"


# --- detect / check_auth / available_models ----------------------------------


def test_detect_when_installed() -> None:
    probe = FakeProbe(
        which_map={"qwen": "/usr/bin/qwen"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="0.17.0"),
    )
    result = QwenAdapter(probe=probe).detect()
    assert result.installed
    assert result.path == "/usr/bin/qwen"
    assert result.version == "0.17.0"


def test_detect_when_absent() -> None:
    adapter = QwenAdapter(probe=FakeProbe(which_map={}))
    assert not adapter.detect().installed


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    status = QwenAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_unknown_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    status = QwenAdapter(probe=FakeProbe()).check_auth()
    # qwen-oauth may still be valid, so we never report NEEDS_LOGIN.
    assert status.state is AuthState.UNKNOWN


def test_available_models_static() -> None:
    assert QwenAdapter().available_models() == []
