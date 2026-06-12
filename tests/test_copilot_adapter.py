# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the GitHub Copilot CLI adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.copilot import CopilotAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "copilot"

_TOKEN_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="copilot", model="claude-sonnet-4.5"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="copilot", model="claude-sonnet-4.5"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def _clear_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _TOKEN_VARS:
        monkeypatch.delenv(var, raising=False)


# --- build_invocation --------------------------------------------------------


def test_build_invocation_prompt_is_the_p_argv_value() -> None:
    spec = CopilotAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[0] == "copilot"
    assert spec.argv[spec.argv.index("-p") + 1] == "say hi"
    assert spec.stdin is None
    assert "--output-format" in spec.argv
    assert spec.argv[spec.argv.index("--output-format") + 1] == "json"
    assert "--no-auto-update" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "claude-sonnet-4.5"


def test_build_invocation_working_dir_and_resume() -> None:
    spec = CopilotAdapter().build_invocation(_req(working_dir="/work", session_id="sess-1"), _ctx())
    assert spec.argv[spec.argv.index("-C") + 1] == "/work"
    assert "--add-dir" in spec.argv
    assert "--resume=sess-1" in spec.argv
    assert spec.cwd == "/work"


def test_build_invocation_folds_preamble_and_files_into_prompt() -> None:
    spec = CopilotAdapter().build_invocation(_req(files=["a.py"]), _ctx(preamble="You are a reviewer."))
    prompt = spec.argv[spec.argv.index("-p") + 1]
    assert prompt.startswith("You are a reviewer.")
    assert "Files in scope:" in prompt
    assert "- a.py" in prompt


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = CopilotAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    assert spec.argv[spec.argv.index("-p") + 1] == "rm -rf / ; echo pwned"
    assert all(";" not in arg for arg in spec.argv if arg != "rm -rf / ; echo pwned")


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = CopilotAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == [
        "--allow-all-tools",
        "--deny-tool",
        "write",
        "--deny-tool",
        "shell",
        "--no-ask-user",
    ]
    assert flags[SafetyMode.PROPOSE].args == flags[SafetyMode.READ_ONLY].args
    assert flags[SafetyMode.WRITE].args == ["--allow-all-tools", "--deny-tool", "shell", "--no-ask-user"]
    assert flags[SafetyMode.YOLO].args == ["--yolo", "--no-ask-user"]
    # read modes deny write; only yolo carries the bypass.
    assert "write" in flags[SafetyMode.READ_ONLY].args
    assert "--yolo" not in flags[SafetyMode.READ_ONLY].args
    assert "--yolo" not in flags[SafetyMode.WRITE].args


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=5.6)
    result = CopilotAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "OK"
    assert result.session_id == "43cae225-8c4c-42ad-ae01-37aadce41a34"
    # Token/USD cost is not in the prompt-mode stream (it lives in OTEL side-channel files).
    assert result.cost is None


def test_parse_error_event_golden_preserves_specific_message() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error.jsonl"), duration_s=1.0)
    result = CopilotAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    # The specific `error` event message wins over the generic result.exitCode fallback.
    assert "rate limited" in result.error.message.lower()


def test_parse_nonzero_exit_with_no_events() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr="no valid GitHub token found", duration_s=0.3)
    result = CopilotAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "token" in result.error.message


def test_parse_no_assistant_message_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"type":"result","sessionId":"s1","exitCode":0}', duration_s=0.2)
    result = CopilotAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_timeout_is_timeout() -> None:
    result = CopilotAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


# --- detect / check_auth / available_models ----------------------------------


def test_check_auth_with_env_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "ghs_validfinegrained")
    status = CopilotAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_classic_ghp_token_is_needs_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "ghp_classictoken")
    status = CopilotAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.NEEDS_LOGIN
    assert "fine-grained" in (status.detail or "")


def test_check_auth_with_persisted_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_tokens(monkeypatch)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    status = CopilotAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_needs_login_without_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_tokens(monkeypatch)
    status = CopilotAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.NEEDS_LOGIN


def test_available_models_static() -> None:
    assert CopilotAdapter().available_models() == ["auto"]
