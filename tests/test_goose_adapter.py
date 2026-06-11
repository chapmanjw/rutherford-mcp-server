# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Goose adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.goose import GooseAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "goose"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="goose", model="claude-sonnet"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="goose", model="claude-sonnet"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = GooseAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:3] == ["goose", "run", "-q"]
    # No session id -> one-shot --no-session, then the prompt via -t.
    assert "--no-session" in spec.argv
    assert spec.argv[spec.argv.index("-t") + 1] == "say hi"
    assert "--model" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "claude-sonnet"
    # No --output-format flag (json schema is unstable).
    assert "--output-format" not in spec.argv
    # No working-dir flag exists for goose.
    assert "--add-dir" not in spec.argv


def test_build_invocation_sets_cwd_without_a_dir_flag() -> None:
    spec = GooseAdapter().build_invocation(_req(working_dir="/work"), _ctx())
    assert spec.cwd == "/work"
    # The working dir is carried only on cwd, never as an argv flag.
    assert "/work" not in spec.argv


def test_build_invocation_resumes_named_session() -> None:
    spec = GooseAdapter().build_invocation(_req(session_id="sess-1"), _ctx())
    assert spec.argv[spec.argv.index("-n") + 1] == "sess-1"
    assert "-r" in spec.argv
    assert "--no-session" not in spec.argv


def test_build_invocation_uses_system_flag_for_role() -> None:
    spec = GooseAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert "--system" in spec.argv
    assert spec.argv[spec.argv.index("--system") + 1] == "You are a reviewer."
    # The preamble is not also folded into the prompt.
    assert spec.argv[spec.argv.index("-t") + 1] == "say hi"


def test_build_invocation_appends_files_to_prompt() -> None:
    spec = GooseAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    prompt = spec.argv[spec.argv.index("-t") + 1]
    assert "Files in scope:" in prompt
    assert "- a.py" in prompt


def test_build_invocation_overlays_safety_env() -> None:
    spec = GooseAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert spec.env.get("GOOSE_MODE") == "auto"


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = GooseAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text is a single argv element, never concatenated into a command line.
    assert "rm -rf / ; echo pwned" in spec.argv
    assert all(";" not in arg or arg == "rm -rf / ; echo pwned" for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = GooseAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].env == {"GOOSE_MODE": "smart_approve"}
    assert flags[SafetyMode.PROPOSE].env == {"GOOSE_MODE": "smart_approve"}
    assert flags[SafetyMode.WRITE].env == {"GOOSE_MODE": "auto"}
    assert flags[SafetyMode.YOLO].env == {"GOOSE_MODE": "auto"}
    # Approval is via env only; no flags are emitted, so nothing defaults to a bypass flag.
    assert all(flags[mode].args == [] for mode in SafetyMode)


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.txt"), duration_s=2.3)
    result = GooseAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id is None
    assert result.error is None


def test_parse_empty_stdout_on_zero_exit_is_success() -> None:
    raw = ProcessResult(exit_code=0, stdout="", stderr="some diagnostics", duration_s=0.2)
    result = GooseAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == ""


def test_parse_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr=_sample("error.txt"), duration_s=0.4)
    result = GooseAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "provider" in result.error.message.lower()


# --- detect / check_auth / available_models ----------------------------------


def test_check_auth_with_provider_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOSE_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    status = GooseAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_via_info_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="config ok"))
    assert GooseAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="not configured"))
    assert GooseAdapter(probe=probe).check_auth().state is AuthState.UNKNOWN


def test_available_models_static_is_empty() -> None:
    assert GooseAdapter().available_models() == []
