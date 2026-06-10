# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Cursor adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.cursor import CursorAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "cursor"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="cursor", model="auto"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="cursor", model="auto"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = CursorAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:5] == ["cursor-agent", "-p", "--output-format", "json", "--trust"]
    assert "--model" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "auto"


def test_build_invocation_always_includes_trust() -> None:
    spec = CursorAdapter().build_invocation(_req(), _ctx())
    # --trust is required in headless mode or the CLI blocks on a workspace-trust prompt.
    assert "--trust" in spec.argv


def test_build_invocation_prompt_goes_to_stdin_not_argv() -> None:
    spec = CursorAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv


def test_build_invocation_includes_workspace_and_resume() -> None:
    spec = CursorAdapter().build_invocation(
        _req(working_dir="/work", session_id="sess-1"),
        _ctx(),
    )
    assert "--workspace" in spec.argv
    assert spec.argv[spec.argv.index("--workspace") + 1] == "/work"
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("--resume") + 1] == "sess-1"


def test_build_invocation_folds_role_preamble_into_stdin() -> None:
    spec = CursorAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a reviewer.")
    assert "say hi" in spec.stdin
    # Cursor has no system-prompt flag, so the preamble is never an argv element.
    assert "You are a reviewer." not in spec.argv


def test_build_invocation_appends_files_to_stdin() -> None:
    spec = CursorAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    assert spec.stdin is not None
    assert "Files in scope:" in spec.stdin
    assert "- a.py" in spec.stdin


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = CursorAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text rides in stdin as a single string; it is never an argv element.
    assert spec.stdin == "rm -rf / ; echo pwned"
    assert all(";" not in arg for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = CursorAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == ["--mode", "ask"]
    assert flags[SafetyMode.PROPOSE].args == ["--mode", "plan"]
    assert flags[SafetyMode.WRITE].args == []
    assert flags[SafetyMode.YOLO].args == ["--force"]


def test_map_safety_fails_closed_on_an_unknown_mode() -> None:
    # Cursor's print default is edit-capable, so the catch-all must be the RESTRICTIVE branch:
    # a SafetyMode value this adapter does not know (a future, likely more-restrictive mode) gets
    # --mode ask, never the edit-capable default. Exercised with a stand-in enum member since a
    # real unknown SafetyMode cannot be constructed today -- the assertion pins the fall-through.
    class _FutureMode:
        value = "audit"

    flags = CursorAdapter().map_safety(_FutureMode())  # type: ignore[arg-type]
    assert flags.args == ["--mode", "ask"]


def test_build_invocation_read_only_adds_ask_mode() -> None:
    spec = CursorAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.READ_ONLY))
    assert spec.argv[spec.argv.index("--mode") + 1] == "ask"


def test_build_invocation_yolo_adds_force() -> None:
    spec = CursorAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.YOLO))
    assert "--force" in spec.argv


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.json"), duration_s=2.3)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "d829d3c9-f83a-40a2-88f8-683f7afd42fe"
    assert result.cost is not None
    assert result.cost.input_tokens == 4739
    assert result.cost.output_tokens == 19


def test_parse_in_band_error_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("error.json"), duration_s=1.2)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert "free plan" in result.text.lower()


def test_parse_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=2, stdout="", stderr="error: not authenticated", duration_s=0.4)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "authenticated" in result.error.message


def test_parse_timeout() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, duration_s=300.0)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not json at all", duration_s=0.1)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


# --- parse_output: output-drift regression tests -----------------------------


def test_parse_result_absent_is_parse_error() -> None:
    """A success envelope with no `result` key must yield PARSE_ERROR, not ok=True."""
    payload = '{"session_id": "abc", "subtype": "success", "is_error": false}'
    raw = ProcessResult(exit_code=0, stdout=payload, duration_s=1.0)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_result_null_is_parse_error_not_none_string() -> None:
    """`result: null` must yield PARSE_ERROR; the text field must not be the string 'None'."""
    payload = '{"result": null, "session_id": "abc", "subtype": "success", "is_error": false}'
    raw = ProcessResult(exit_code=0, stdout=payload, duration_s=1.0)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.text != "None"


def test_parse_nonempty_result_still_succeeds() -> None:
    """The happy path: a non-empty `result` string must still yield ok=True."""
    payload = '{"result": "hello world", "session_id": "abc", "subtype": "success", "is_error": false}'
    raw = ProcessResult(exit_code=0, stdout=payload, duration_s=1.0)
    result = CursorAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "hello world"
    assert result.session_id == "abc"


# --- check_output_contract ---------------------------------------------------


def test_check_output_contract_passes_with_json() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"result": "ok"}', duration_s=1.0)
    assert CursorAdapter().check_output_contract(raw) is True


def test_check_output_contract_fails_without_json() -> None:
    raw = ProcessResult(exit_code=0, stdout="plain text output", duration_s=1.0)
    assert CursorAdapter().check_output_contract(raw) is False


# --- detect / check_auth / available_models ----------------------------------


def test_detect_when_installed() -> None:
    probe = FakeProbe(
        which_map={"cursor-agent": "/usr/bin/cursor-agent"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="2026.05.28"),
    )
    result = CursorAdapter(probe=probe).detect()
    assert result.installed
    assert result.path == "/usr/bin/cursor-agent"
    assert result.version == "2026.05.28"


def test_detect_when_absent() -> None:
    adapter = CursorAdapter(probe=FakeProbe(which_map={}))
    assert not adapter.detect().installed


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "key-test")
    status = CursorAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="logged in"))
    assert CursorAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="not logged in"))
    assert CursorAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN


def test_available_models_lists_via_probe() -> None:
    probe = FakeProbe(
        run_fn=lambda argv: ProcessResult(
            exit_code=0,
            stdout="Available models\n\nauto - Auto\ngpt-5.2 - GPT-5.2\n\nTip: use --model",
        ),
    )
    assert CursorAdapter(probe=probe).available_models() == ["auto", "gpt-5.2"]


def test_available_models_falls_back_on_failure() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="boom"))
    assert CursorAdapter(probe=probe).available_models() == []
