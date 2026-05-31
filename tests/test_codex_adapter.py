# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Codex adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.codex import CodexAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "codex"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="codex", model="gpt-5-codex"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="codex", model="gpt-5-codex"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = CodexAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert "-m" in spec.argv
    assert spec.argv[spec.argv.index("-m") + 1] == "gpt-5-codex"


def test_build_invocation_prompt_goes_to_stdin_not_argv() -> None:
    spec = CodexAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv


def test_build_invocation_includes_working_dir() -> None:
    spec = CodexAdapter().build_invocation(_req(working_dir="/work"), _ctx())
    assert "-C" in spec.argv
    assert spec.argv[spec.argv.index("-C") + 1] == "/work"
    assert spec.cwd == "/work"


def test_build_invocation_resume_uses_resume_subcommand() -> None:
    spec = CodexAdapter().build_invocation(_req(session_id="th-1"), _ctx())
    assert spec.argv[:4] == ["codex", "exec", "resume", "th-1"]
    assert spec.argv[4:6] == ["--json", "--skip-git-repo-check"]


def test_build_invocation_folds_role_preamble_into_stdin() -> None:
    spec = CodexAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a reviewer.")
    assert "say hi" in spec.stdin
    # Codex has no system-prompt flag, so the preamble is never an argv element.
    assert "You are a reviewer." not in spec.argv


def test_build_invocation_appends_files_to_stdin() -> None:
    spec = CodexAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    assert spec.stdin is not None
    assert "Files in scope:" in spec.stdin
    assert "- a.py" in spec.stdin


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = CodexAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text rides in stdin as a single string; it is never an argv element.
    assert spec.stdin == "rm -rf / ; echo pwned"
    assert all(";" not in arg for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = CodexAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == ["-s", "read-only"]
    assert flags[SafetyMode.PROPOSE].args == ["-s", "read-only"]
    assert flags[SafetyMode.WRITE].args == ["-s", "workspace-write"]
    assert flags[SafetyMode.YOLO].args == ["--dangerously-bypass-approvals-and-sandbox"]


def test_build_invocation_write_mode_adds_workspace_write() -> None:
    spec = CodexAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert "workspace-write" in spec.argv


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=2.3)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "th_01HZX9Q2K3M4N5P6R7S8T9V0W1"
    assert result.cost is not None
    assert result.cost.input_tokens == 1200
    assert result.cost.output_tokens == 45


def test_parse_turn_failed_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("turn_failed.jsonl"), duration_s=1.5)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "rate limit" in result.error.message.lower()


def test_parse_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr="error: not authenticated", duration_s=0.4)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "authenticated" in result.error.message


def test_parse_timeout() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, duration_s=300.0)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_parse_no_agent_message_is_parse_error() -> None:
    raw = ProcessResult(
        exit_code=0,
        stdout='{"type":"thread.started","thread_id":"th-x"}\n{"type":"turn.completed","usage":{}}',
        duration_s=0.1,
    )
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


# --- detect / check_auth / available_models ----------------------------------


def test_detect_when_installed() -> None:
    probe = FakeProbe(
        which_map={"codex": "/usr/bin/codex"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="codex-cli 0.135.0"),
    )
    result = CodexAdapter(probe=probe).detect()
    assert result.installed
    assert result.path == "/usr/bin/codex"
    assert result.version == "codex-cli 0.135.0"


def test_detect_when_absent() -> None:
    adapter = CodexAdapter(probe=FakeProbe(which_map={}))
    assert not adapter.detect().installed


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    status = CodexAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_persisted_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    status = CodexAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED
    assert status.detail == "persisted session"


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    status = CodexAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.NEEDS_LOGIN


def test_available_models_static() -> None:
    assert CodexAdapter().available_models() == []
