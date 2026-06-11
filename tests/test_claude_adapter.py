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


@pytest.fixture(autouse=True)
def _clear_backend_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep check_auth tests hermetic regardless of the dev's shell: clear the provider switches."""
    for var in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    ):
        monkeypatch.delenv(var, raising=False)


# --- build_invocation --------------------------------------------------------


def test_build_invocation_prompt_rides_on_stdin_not_argv() -> None:
    # The panel's MAJOR: a prompt on argv hits the ~32K Windows command-line cap on long debate
    # rounds and the cmd.exe newline truncation through an npm .cmd shim. The prompt must be the
    # stdin payload, with no positional after -p (which makes claude read stdin).
    spec = ClaudeCodeAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv
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


def test_build_invocation_folds_the_role_preamble_into_the_stdin_prompt() -> None:
    # The preamble is multiline by nature; --append-system-prompt would put it on argv where a
    # .cmd-shim launch truncates at the first newline. It composes into the stdin prompt instead,
    # the same way every other stdin-transport adapter does.
    spec = ClaudeCodeAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    assert "--append-system-prompt" not in spec.argv
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a reviewer.")
    assert "say hi" in spec.stdin


def test_build_invocation_multiline_prompt_never_touches_argv() -> None:
    multiline = "line one\nline two\n\nline four"
    spec = ClaudeCodeAdapter().build_invocation(_req(prompt=multiline), _ctx(preamble="role\nwith lines"))
    assert spec.stdin is not None and multiline in spec.stdin
    assert all("\n" not in arg for arg in spec.argv)  # nothing newline-bearing rides argv


def test_build_invocation_appends_files_to_prompt() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    assert spec.stdin is not None
    assert "Files in scope:" in spec.stdin
    assert "- a.py" in spec.stdin


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = ClaudeCodeAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text rides on stdin, never concatenated into a command line.
    assert spec.stdin is not None and "rm -rf / ; echo pwned" in spec.stdin
    assert all(";" not in arg for arg in spec.argv)


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


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not json at all", duration_s=0.1)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_result_null_is_parse_error_not_none_string() -> None:
    # Regression: `result: null` in the JSON envelope must not produce ok=True with text="None".
    stdout = '{"type":"result","subtype":"success","is_error":false,"result":null,"session_id":"abc"}'
    raw = ProcessResult(exit_code=0, stdout=stdout, duration_s=1.0)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.text != "None"


def test_parse_result_absent_is_parse_error() -> None:
    # Regression: a success envelope with no `result` key must not return ok=True with empty text.
    stdout = '{"type":"result","subtype":"success","is_error":false,"session_id":"abc"}'
    raw = ProcessResult(exit_code=0, stdout=stdout, duration_s=1.0)
    result = ClaudeCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


# --- detect / check_auth / available_models ----------------------------------


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


# --- check_auth: third-party cloud backends (Bedrock / Vertex / Mantle) -------

#: The exact `claude auth status` JSON observed on a Bedrock-configured machine.
_BEDROCK_STATUS = '{"loggedIn": true, "authMethod": "third_party", "apiProvider": "bedrock"}'


def _auth_status_probe(stdout: str = "", *, exit_code: int = 0) -> FakeProbe:
    """A probe whose `claude auth status` returns the given output; other calls are inert."""

    def run_fn(argv: list[str]) -> ProcessResult:
        if "auth" in argv:  # ["claude", "auth", "status"]
            return ProcessResult(exit_code=exit_code, stdout=stdout)
        return ProcessResult(exit_code=0, stdout="")

    return FakeProbe(which_map={"claude": "/usr/bin/claude"}, run_fn=run_fn)


def test_check_auth_bedrock_provider_defers_to_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # apiProvider=bedrock (authMethod third_party): `loggedIn` only means *configured*, so the cheap
    # probe must report UNKNOWN and let doctor's live check confirm the AWS creds actually reach a model.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = ClaudeCodeAdapter(probe=_auth_status_probe(_BEDROCK_STATUS)).check_auth()
    assert status.state is AuthState.UNKNOWN
    assert "bedrock" in (status.detail or "").lower()


def test_check_auth_vertex_provider_defers_to_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = ClaudeCodeAdapter(probe=_auth_status_probe('{"loggedIn": true, "apiProvider": "vertex"}')).check_auth()
    assert status.state is AuthState.UNKNOWN


def test_check_auth_third_party_auth_method_defers_to_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with an apiProvider we don't enumerate, authMethod=third_party is enough to defer.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    probe = _auth_status_probe('{"loggedIn": true, "authMethod": "third_party", "apiProvider": "acme-gateway"}')
    assert ClaudeCodeAdapter(probe=probe).check_auth().state is AuthState.UNKNOWN


def test_check_auth_bedrock_env_var_defers_even_without_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # An older CLI whose `auth status` emits no JSON: the CLAUDE_CODE_USE_BEDROCK switch alone is
    # enough to know the Anthropic-login probe is the wrong signal.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    status = ClaudeCodeAdapter(probe=_auth_status_probe("", exit_code=1)).check_auth()
    assert status.state is AuthState.UNKNOWN


def test_check_auth_bedrock_env_wins_over_stray_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # With a backend switch set, Claude Code ignores ANTHROPIC_API_KEY, so a stray key must not
    # produce a false AUTHENTICATED -- the backend still defers to the live check.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stray")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "true")
    status = ClaudeCodeAdapter(probe=_auth_status_probe("", exit_code=1)).check_auth()
    assert status.state is AuthState.UNKNOWN


def test_check_auth_first_party_logged_in_is_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    probe = _auth_status_probe('{"loggedIn": true, "authMethod": "claudeai", "apiProvider": "anthropic"}')
    assert ClaudeCodeAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_json_not_logged_in_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = ClaudeCodeAdapter(probe=_auth_status_probe('{"loggedIn": false}')).check_auth()
    assert status.state is AuthState.NEEDS_LOGIN


def test_check_auth_parses_pretty_printed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pretty = '{\n  "loggedIn": true,\n  "authMethod": "third_party",\n  "apiProvider": "bedrock"\n}'
    assert ClaudeCodeAdapter(probe=_auth_status_probe(pretty)).check_auth().state is AuthState.UNKNOWN


def test_capabilities_no_longer_claim_system_prompt_support() -> None:
    # Pins the deliberate flip: the role preamble folds into the stdin prompt (the CLI's
    # --append-system-prompt is an argv element a .cmd-shim launch can truncate at a newline), so
    # the adapter must not advertise system-prompt support it no longer uses.
    assert ClaudeCodeAdapter().capabilities().supports_system_prompt is False
