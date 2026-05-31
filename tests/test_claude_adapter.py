# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Claude Code reference adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.claude_code import ClaudeCodeAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "claude_code"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="claude_code", model="opus"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="claude_code", model="opus"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:5] == ["claude", "-p", "say hi", "--output-format", "json"]
    assert "--model" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "opus"


def test_build_invocation_includes_working_dir_and_resume() -> None:
    spec = ClaudeCodeAdapter().build_invocation(
        _req(working_dir="/work", session_id="sess-1"),
        _ctx(),
    )
    assert "--add-dir" in spec.argv
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("--resume") + 1] == "sess-1"


def test_build_invocation_uses_system_prompt_for_role() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert "--append-system-prompt" in spec.argv
    assert spec.argv[spec.argv.index("--append-system-prompt") + 1] == "You are a reviewer."


def test_build_invocation_appends_files_to_prompt() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    prompt = spec.argv[2]
    assert "Files in scope:" in prompt
    assert "- a.py" in prompt


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text is a single argv element, never concatenated into a command line.
    assert "rm -rf / ; echo pwned" in spec.argv
    assert all(";" not in arg or arg == "rm -rf / ; echo pwned" for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = ClaudeCodeAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == []
    assert flags[SafetyMode.PROPOSE].args == []
    assert flags[SafetyMode.WRITE].args == ["--permission-mode", "acceptEdits"]
    assert flags[SafetyMode.YOLO].args == ["--dangerously-skip-permissions"]


def test_build_invocation_write_mode_adds_accept_edits() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert "acceptEdits" in spec.argv


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.json"), duration_s=2.3)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "5f3b9c1a-2e7d-4a8b-9c6e-1d2f3a4b5c6d"
    assert result.cost is not None
    assert result.cost.usd == 0.0123
    assert result.cost.input_tokens == 1200


def test_parse_in_band_error_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error_max_turns.json"), duration_s=41.0)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "max" in result.text.lower()


def test_parse_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr=_sample("nonzero_stderr.txt"), duration_s=0.4)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "API key" in result.error.message


def test_parse_timeout() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, duration_s=300.0)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not json at all", duration_s=0.1)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


# --- detect / check_auth / available_models ----------------------------------


def test_detect_when_installed() -> None:
    probe = FakeProbe(
        which_map={"claude": "/usr/bin/claude"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="2.1.158 (Claude Code)"),
    )
    result = ClaudeCodeAdapter(probe=probe).detect()
    assert result.installed
    assert result.path == "/usr/bin/claude"
    assert result.version == "2.1.158 (Claude Code)"


def test_detect_when_absent() -> None:
    adapter = ClaudeCodeAdapter(probe=FakeProbe(which_map={}))
    assert not adapter.detect().installed


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    status = ClaudeCodeAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="logged in"))
    assert ClaudeCodeAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="not logged in"))
    assert ClaudeCodeAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN


def test_available_models_static() -> None:
    assert ClaudeCodeAdapter().available_models() == ["opus", "sonnet", "haiku"]
