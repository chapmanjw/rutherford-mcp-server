# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Factory Droid adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.droid import DroidAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "droid"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="droid", model="claude-opus-4-8"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="droid", model="claude-opus-4-8"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = DroidAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:4] == ["droid", "exec", "--output-format", "json"]
    assert spec.argv[spec.argv.index("-m") + 1] == "claude-opus-4-8"


def test_build_invocation_prompt_goes_to_stdin_not_argv() -> None:
    spec = DroidAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv


def test_build_invocation_includes_working_dir_and_resume() -> None:
    spec = DroidAdapter().build_invocation(_req(working_dir="/work", session_id="sess-1"), _ctx())
    assert spec.argv[spec.argv.index("--cwd") + 1] == "/work"
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("-s") + 1] == "sess-1"


def test_build_invocation_folds_role_preamble_and_files_into_stdin() -> None:
    spec = DroidAdapter().build_invocation(_req(files=["a.py"]), _ctx(preamble="You are a reviewer."))
    assert "--append-system-prompt" not in spec.argv
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a reviewer.")
    assert "Files in scope:" in spec.stdin
    assert "- a.py" in spec.stdin


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = DroidAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    assert spec.stdin == "rm -rf / ; echo pwned"
    assert all(";" not in arg for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = DroidAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == []
    assert flags[SafetyMode.PROPOSE].args == []
    assert flags[SafetyMode.WRITE].args == ["--auto", "low"]
    assert flags[SafetyMode.YOLO].args == ["--skip-permissions-unsafe"]
    # No bypass flag is ever in a non-mutating mode.
    assert "--skip-permissions-unsafe" not in flags[SafetyMode.READ_ONLY].args
    assert "--skip-permissions-unsafe" not in flags[SafetyMode.PROPOSE].args


def test_build_invocation_write_mode_adds_auto_low() -> None:
    spec = DroidAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert spec.argv[-2:] == ["--auto", "low"]


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.json"), duration_s=3.7)
    result = DroidAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "OK"
    assert result.session_id == "512e2220-3560-47b4-896c-31bd855c6bcb"
    # Token counts come from the nested usage block; this build emits no total_cost_usd.
    assert result.cost is not None
    assert result.cost.input_tokens == 11492
    assert result.cost.output_tokens == 52
    assert result.cost.usd is None


def test_parse_in_band_error_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error.json"), duration_s=1.2)
    result = DroidAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "maximum number of turns" in result.error.message.lower()


def test_parse_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr="Error: not authenticated", duration_s=0.4)
    result = DroidAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "authenticated" in result.error.message


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not json at all", duration_s=0.1)
    result = DroidAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_timeout_is_timeout() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True)
    result = DroidAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


# --- detect / check_auth / available_models ----------------------------------


def test_check_auth_with_env_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FACTORY_API_KEY", "fk-test")
    status = DroidAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_persisted_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    monkeypatch.delenv("FACTORY_TOKEN", raising=False)
    (tmp_path / "auth.v2.file").write_text("{}", encoding="utf-8")
    status = DroidAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_needs_login_without_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    monkeypatch.delenv("FACTORY_TOKEN", raising=False)
    status = DroidAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.NEEDS_LOGIN


def test_available_models_static() -> None:
    assert DroidAdapter().available_models() == []
