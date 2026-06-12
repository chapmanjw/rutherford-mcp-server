# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Mistral Vibe adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.vibe import VibeAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "vibe"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="vibe", model=None),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="vibe", model=None), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_prompt_is_the_p_argv_value() -> None:
    spec = VibeAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[0] == "vibe"
    assert spec.argv[spec.argv.index("-p") + 1] == "say hi"
    # -p is last so the prompt is the final token.
    assert spec.argv.index("-p") == len(spec.argv) - 2
    assert "--output" in spec.argv and spec.argv[spec.argv.index("--output") + 1] == "json"
    assert "--trust" in spec.argv


def test_build_invocation_forces_utf8_stdout_env() -> None:
    spec = VibeAdapter().build_invocation(_req(), _ctx())
    # Without this, vibe crashes on Windows on any non-cp1252 answer character.
    assert spec.env.get("PYTHONIOENCODING") == "utf-8"
    assert spec.env.get("PYTHONUTF8") == "1"


def test_build_invocation_model_rides_on_env_not_a_flag() -> None:
    spec = VibeAdapter().build_invocation(
        DelegationRequest(target=Target(cli="vibe", model="devstral-small"), prompt="say hi"),
        InvocationContext(target=Target(cli="vibe", model="devstral-small"), correlation_id="t"),
    )
    assert "--model" not in spec.argv
    assert spec.env.get("VIBE_ACTIVE_MODEL") == "devstral-small"


def test_build_invocation_working_dir() -> None:
    spec = VibeAdapter().build_invocation(_req(working_dir="/work"), _ctx())
    assert spec.argv[spec.argv.index("--workdir") + 1] == "/work"
    assert spec.cwd == "/work"


def test_build_invocation_folds_preamble_and_files_into_prompt() -> None:
    spec = VibeAdapter().build_invocation(_req(files=["a.py"]), _ctx(preamble="You are a reviewer."))
    prompt = spec.argv[spec.argv.index("-p") + 1]
    assert prompt.startswith("You are a reviewer.")
    assert "- a.py" in prompt
    assert spec.stdin is None  # no stdin: the runner attaches DEVNULL, the EOF vibe -p waits for


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = VibeAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    assert spec.argv[spec.argv.index("-p") + 1] == "rm -rf / ; echo pwned"


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = VibeAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == ["--agent", "plan"]
    assert flags[SafetyMode.PROPOSE].args == ["--agent", "plan"]
    assert flags[SafetyMode.WRITE].args == ["--agent", "accept-edits"]
    assert flags[SafetyMode.YOLO].args == ["--agent", "auto-approve"]
    assert "auto-approve" not in flags[SafetyMode.READ_ONLY].args


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.json"), duration_s=2.3)
    result = VibeAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "OK"
    # The array carries no session id or cost figure.
    assert result.session_id is None
    assert result.cost is None


def test_parse_no_assistant_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("no_assistant.json"), duration_s=1.0)
    result = VibeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_nonzero_exit_is_failure() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr="Error: MISTRAL_API_KEY not set", duration_s=0.3)
    result = VibeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "mistral_api_key" in result.error.message.lower()


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not a json array", duration_s=0.1)
    result = VibeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_crash_truncated_array_fails_cleanly() -> None:
    # The cp1252 charmap crash leaves a truncated array; it must fail as a non-zero exit, not a
    # silent wrong answer. (The PYTHONIOENCODING env in build_invocation prevents the crash itself.)
    raw = ProcessResult(
        exit_code=1, stdout='[\n  {\n    "role": "system",\n    "content":', stderr="charmap", duration_s=0.2
    )
    result = VibeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


def test_parse_timeout_is_timeout() -> None:
    result = VibeAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


# --- detect / check_auth / available_models ----------------------------------


def test_check_auth_with_env_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "mk-test")
    status = VibeAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_persisted_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    (tmp_path / ".env").write_text("MISTRAL_API_KEY=mk-x", encoding="utf-8")
    status = VibeAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_key_missing_without_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    status = VibeAdapter(probe=FakeProbe(), data_root=tmp_path).check_auth()
    assert status.state is AuthState.API_KEY_MISSING


def test_available_models_static() -> None:
    assert VibeAdapter().available_models() == []
