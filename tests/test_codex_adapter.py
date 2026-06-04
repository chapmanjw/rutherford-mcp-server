# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Codex adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.codex import CodexAdapter
from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeProbe, FakeProcessRunner

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
    # codex exec resume [OPTIONS] -- <SESSION_ID> -  (session id + stdin-prompt sentinel, both
    # positional after a -- guard). Verified against `codex exec resume --help` (codex-cli 0.135.0).
    spec = CodexAdapter().build_invocation(_req(session_id="th-1"), _ctx())
    assert spec.argv[:3] == ["codex", "exec", "resume"]
    # the session id is a positional after --, never behind a flag, and never a -s value
    assert "--" in spec.argv
    sep = spec.argv.index("--")
    assert spec.argv[sep + 1 :] == ["th-1", "-"]
    assert "-s" not in spec.argv  # resume rejects -s/--sandbox


def test_build_invocation_resume_has_no_sandbox_or_cd_flags_read_only() -> None:
    # resume rejects -s/--sandbox and -C/--cd; read-only posture is expressed via -c sandbox_mode=
    spec = CodexAdapter().build_invocation(_req(session_id="th-1", working_dir="/work"), _ctx())
    assert "-s" not in spec.argv
    assert "-C" not in spec.argv  # working dir comes from the process cwd, not a flag resume rejects
    assert spec.cwd == "/work"
    assert "-c" in spec.argv
    assert spec.argv[spec.argv.index("-c") + 1] == "sandbox_mode=read-only"


def test_build_invocation_resume_prompt_rides_on_stdin_via_dash() -> None:
    spec = CodexAdapter().build_invocation(_req(session_id="th-1", prompt="continue please"), _ctx())
    assert spec.stdin == "continue please"
    # The prompt is never a positional argv element (cmd.exe /c shim would truncate a newline);
    # the only prompt positional is the "-" stdin sentinel.
    assert "continue please" not in spec.argv
    assert spec.argv[-1] == "-"


def test_build_invocation_resume_includes_model() -> None:
    spec = CodexAdapter().build_invocation(_req(session_id="th-1"), _ctx())
    assert "-m" in spec.argv
    assert spec.argv[spec.argv.index("-m") + 1] == "gpt-5-codex"
    # -m must precede the -- separator so it is parsed as an option, not a positional
    assert spec.argv.index("-m") < spec.argv.index("--")


def test_build_invocation_resume_write_uses_workspace_write_config() -> None:
    spec = CodexAdapter().build_invocation(_req(session_id="th-1"), _ctx(safety=SafetyMode.WRITE))
    assert "-s" not in spec.argv
    assert spec.argv[spec.argv.index("-c") + 1] == "sandbox_mode=workspace-write"


def test_build_invocation_resume_yolo_uses_bypass_flag() -> None:
    spec = CodexAdapter().build_invocation(_req(session_id="th-1"), _ctx(safety=SafetyMode.YOLO))
    assert "--dangerously-bypass-approvals-and-sandbox" in spec.argv
    assert "-s" not in spec.argv
    assert "sandbox_mode=read-only" not in spec.argv  # yolo is the bypass flag, not a config override


def test_build_invocation_resume_guards_dash_prefixed_prompt() -> None:
    # A prompt that begins with '-' must never be parsed as a flag: it stays on stdin, and the
    # only positionals (session id, "-") sit behind the -- separator.
    spec = CodexAdapter().build_invocation(_req(session_id="th-1", prompt="--help me refactor"), _ctx())
    assert spec.stdin == "--help me refactor"
    assert "--help me refactor" not in spec.argv
    sep = spec.argv.index("--")
    assert spec.argv[sep + 1 :] == ["th-1", "-"]


def test_build_invocation_resume_with_empty_prompt() -> None:
    # An empty prompt is supported (stdin=""): the argv shape and -- guard must still hold.
    spec = CodexAdapter().build_invocation(_req(session_id="th-1", prompt=""), _ctx())
    assert spec.stdin == ""
    sep = spec.argv.index("--")
    assert spec.argv[sep + 1 :] == ["th-1", "-"]
    assert "-s" not in spec.argv
    assert "-C" not in spec.argv


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


# The exact clap error a rejected resume emits (captured from codex-cli 0.135.0).
_RESUME_PARSE_STDERR = (
    "error: unexpected argument '-s' found\n\n"
    "  tip: to pass '-s' as a value, use '-- -s'\n\n"
    "Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]\n\n"
    "For more information, try '--help'."
)


def test_parse_resume_argument_error_is_resume_failed_not_silent() -> None:
    raw = ProcessResult(exit_code=2, stdout="", stderr=_RESUME_PARSE_STDERR, duration_s=0.05)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    # A distinct code, not an opaque NONZERO_EXIT, so a lost resume is not silently swallowed.
    assert result.error.code == "RESUME_FAILED"
    assert "codex exec resume" in result.error.message
    assert "-s" in result.error.message  # the offending argument is surfaced
    assert result.text == ""


@pytest.mark.parametrize(
    "clap_message",
    [
        "error: unexpected argument '-s' found",
        "error: unrecognized subcommand 'resme'",
        "error: the following required arguments were not provided:",
        "error: the argument '--all' cannot be used with '--last'",
        "error: invalid value 'nope' for '--model'",
    ],
)
def test_parse_resume_clap_errors_without_help_line_are_resume_failed(clap_message: str) -> None:
    # Robustness against codex version drift: a clap parse error on resume must be detected even if
    # a future build drops or rewords the "For more information, try '--help'." hint line.
    stderr = f"{clap_message}\n\nUsage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]"
    raw = ProcessResult(exit_code=2, stdout="", stderr=stderr, duration_s=0.05)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "RESUME_FAILED"


def test_parse_non_resume_clap_error_stays_nonzero() -> None:
    # A clap parse error on a fresh `codex exec` (no "exec resume" in the usage) is not a resume
    # failure; it stays NONZERO_EXIT rather than being misclassified as RESUME_FAILED.
    stderr = (
        "error: unexpected argument '--bogus' found\n\n"
        "Usage: codex exec [OPTIONS] [PROMPT]\n\n"
        "For more information, try '--help'."
    )
    raw = ProcessResult(exit_code=2, stdout="", stderr=stderr, duration_s=0.05)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


def test_parse_ordinary_runtime_nonzero_stays_nonzero() -> None:
    # A runtime failure (not an argument-parse error) must not be mistaken for a resume failure.
    raw = ProcessResult(exit_code=1, stdout="", stderr="error: failed to reach the API", duration_s=0.4)
    result = CodexAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


# --- delegate() round trip (the contract that actually broke) ----------------


def _codex_service(runner: FakeProcessRunner) -> DelegationService:
    probe = FakeProbe(
        which_map={"codex": "/usr/bin/codex"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="codex-cli 0.135.0"),
    )
    return DelegationService(
        AdapterRegistry([CodexAdapter(probe=probe)]),
        runner,
        RutherfordConfig(),
        load_roles(),
    )


async def test_delegate_codex_resume_passes_session_id_as_positional_without_s_flag() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout=_sample("success.jsonl")))
    result = await _codex_service(runner).delegate(
        DelegationRequest(
            target=Target(cli="codex", model="gpt-5-codex"),
            prompt="keep going",
            session_id="th_live_42",
        )
    )
    assert result.ok
    spec, _timeout = runner.calls[0]
    argv = spec.argv
    assert argv[:3] == ["codex", "exec", "resume"]
    assert "-s" not in argv  # the bug fixed: no -s flag is emitted on resume
    sep = argv.index("--")
    assert argv[sep + 1] == "th_live_42"  # session id is a positional after --
    assert argv[sep + 2] == "-"  # prompt comes from stdin
    assert "th_live_42" not in argv[:sep]  # never passed behind a flag
    assert spec.stdin == "keep going"


async def test_delegate_codex_fresh_session_is_unchanged() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout=_sample("success.jsonl")))
    result = await _codex_service(runner).delegate(
        DelegationRequest(target=Target(cli="codex", model="gpt-5-codex"), prompt="hello")
    )
    assert result.ok
    spec, _timeout = runner.calls[0]
    assert spec.argv[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert "resume" not in spec.argv
    assert spec.argv[-2:] == ["-s", "read-only"]  # fresh path keeps the -s sandbox flag
    assert spec.stdin == "hello"


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


# --- check_auth via `codex doctor --json` (covers the Bedrock provider) -------

#: A `codex doctor --json` report whose auth.credentials check is healthy.
_DOCTOR_OK = '{"overallStatus": "ok", "checks": {"auth.credentials": {"status": "ok"}}}'
#: The same report shape signalling an auth problem.
_DOCTOR_BAD = '{"overallStatus": "error", "checks": {"auth.credentials": {"status": "error"}}}'


def _doctor_probe(stdout: str = "", *, exit_code: int = 0) -> FakeProbe:
    """A probe whose `codex doctor --json` returns the given output; other calls are inert."""

    def run_fn(argv: list[str]) -> ProcessResult:
        if "doctor" in argv:  # ["codex", "doctor", "--json"]
            return ProcessResult(exit_code=exit_code, stdout=stdout)
        return ProcessResult(exit_code=0, stdout="")

    return FakeProbe(which_map={"codex": "/usr/bin/codex"}, run_fn=run_fn)


def _isolate_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip the cheap markers (env keys + ~/.codex/auth.json) so only `codex doctor` can answer."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_check_auth_bedrock_via_doctor_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The Bedrock case: no OPENAI_API_KEY, no ~/.codex/auth.json -- yet codex doctor reports auth ok
    # (its credential is an AWS bearer token / SDK chain). The pre-fix env+file check missed this.
    _isolate_codex(monkeypatch, tmp_path)
    status = CodexAdapter(probe=_doctor_probe(_DOCTOR_OK)).check_auth()
    assert status.state is AuthState.AUTHENTICATED
    assert "doctor" in (status.detail or "").lower()


def test_check_auth_doctor_reports_problem_needs_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_codex(monkeypatch, tmp_path)
    assert CodexAdapter(probe=_doctor_probe(_DOCTOR_BAD)).check_auth().state is AuthState.NEEDS_LOGIN


def test_check_auth_doctor_unavailable_falls_back_to_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An older codex without `doctor --json` (nothing parseable on stdout): fall back to the env key.
    _isolate_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    status = CodexAdapter(probe=_doctor_probe("", exit_code=2)).check_auth()
    assert status.state is AuthState.AUTHENTICATED
    assert status.detail == "OPENAI_API_KEY is set"


def test_check_auth_doctor_unavailable_falls_back_to_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = _isolate_codex(monkeypatch, tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    status = CodexAdapter(probe=_doctor_probe("")).check_auth()
    assert status.state is AuthState.AUTHENTICATED
    assert status.detail == "persisted session"


def test_check_auth_doctor_json_with_noise_is_parsed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_codex(monkeypatch, tmp_path)
    noisy = f"checking environment...\n{_DOCTOR_OK}\n"
    assert CodexAdapter(probe=_doctor_probe(noisy)).check_auth().state is AuthState.AUTHENTICATED
