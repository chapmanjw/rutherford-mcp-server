# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Kiro adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.kiro import KiroAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "kiro"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="kiro", model="kiro-sonnet"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="kiro", model="kiro-sonnet"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_basic_argv_is_a_list() -> None:
    spec = KiroAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    # The prompt is the positional argument right after --no-interactive.
    assert spec.argv[:4] == ["kiro-cli", "chat", "--no-interactive", "say hi"]
    assert "--model" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "kiro-sonnet"


def test_build_invocation_sets_cwd_not_a_dir_flag() -> None:
    spec = KiroAdapter().build_invocation(_req(working_dir="/work"), _ctx())
    # Kiro has no working-directory flag; the working dir rides on spec.cwd.
    assert spec.cwd == "/work"
    assert "--add-dir" not in spec.argv
    assert "/work" not in spec.argv


def test_build_invocation_includes_resume_id() -> None:
    spec = KiroAdapter().build_invocation(_req(session_id="sess-1"), _ctx())
    assert "--resume-id" in spec.argv
    assert spec.argv[spec.argv.index("--resume-id") + 1] == "sess-1"


def test_build_invocation_folds_role_into_prompt() -> None:
    spec = KiroAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    # No system-prompt flag: the preamble is composed into the positional prompt.
    assert "--append-system-prompt" not in spec.argv
    assert "--system-prompt" not in spec.argv
    prompt = spec.argv[3]
    assert prompt.startswith("You are a reviewer.")
    assert "say hi" in prompt


def test_build_invocation_appends_files_to_prompt() -> None:
    spec = KiroAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    prompt = spec.argv[3]
    assert "Files in scope:" in prompt
    assert "- a.py" in prompt


def test_build_invocation_overlays_safety_env() -> None:
    spec = KiroAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.env, dict)


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = KiroAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text is a single argv element, never concatenated into a command line.
    assert "rm -rf / ; echo pwned" in spec.argv
    assert all(";" not in arg or arg == "rm -rf / ; echo pwned" for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = KiroAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    assert flags[SafetyMode.READ_ONLY].args == ["--trust-tools=fs_read"]
    assert flags[SafetyMode.PROPOSE].args == ["--trust-tools=fs_read"]
    assert flags[SafetyMode.WRITE].args == ["--trust-tools=fs_read,fs_write"]
    assert flags[SafetyMode.YOLO].args == ["--trust-all-tools"]


def test_map_safety_never_defaults_to_bypass() -> None:
    # Every non-yolo mode stays on the scoped allowlist, never --trust-all-tools.
    adapter = KiroAdapter()
    for mode in (SafetyMode.READ_ONLY, SafetyMode.PROPOSE, SafetyMode.WRITE):
        assert "--trust-all-tools" not in adapter.map_safety(mode).args


def test_build_invocation_write_mode_adds_fs_write() -> None:
    spec = KiroAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert "--trust-tools=fs_read,fs_write" in spec.argv


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.txt"), duration_s=2.3)
    result = KiroAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == _sample("success.txt").strip()
    assert "Reversing a string in Python" in result.text
    assert result.session_id is None


def test_parse_nonzero_exit() -> None:
    raw = ProcessResult(
        exit_code=1,
        stdout="",
        stderr="Error: not authenticated; run `kiro-cli login`.",
        duration_s=0.4,
    )
    result = KiroAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "not authenticated" in result.error.message


def test_parse_timeout() -> None:
    raw = ProcessResult(exit_code=None, timed_out=True, duration_s=300.0)
    result = KiroAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


# --- detect / check_auth / available_models ----------------------------------


def test_detect_when_installed() -> None:
    probe = FakeProbe(
        which_map={"kiro-cli": "/usr/bin/kiro-cli"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="kiro-cli 2.5.0"),
    )
    result = KiroAdapter(probe=probe).detect()
    assert result.installed
    assert result.path == "/usr/bin/kiro-cli"
    assert result.version == "kiro-cli 2.5.0"


def test_detect_when_absent() -> None:
    adapter = KiroAdapter(probe=FakeProbe(which_map={}))
    assert not adapter.detect().installed


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIRO_API_KEY", "key-test")
    status = KiroAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout='{"user":"jc"}'))
    assert KiroAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="not logged in"))
    assert KiroAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN


def test_available_models_parses_list_models_json() -> None:
    probe = FakeProbe(
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout=_sample("list_models.json")),
    )
    models = KiroAdapter(probe=probe).available_models()
    assert models == ["kiro-sonnet", "kiro-opus", "kiro-haiku"]
    # The list comes from the documented list-models subcommand.
    assert probe.calls[-1] == ["kiro-cli", "chat", "--list-models", "--format", "json"]


def test_available_models_handles_list_of_strings() -> None:
    probe = FakeProbe(
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout='["alpha", "beta"]'),
    )
    assert KiroAdapter(probe=probe).available_models() == ["alpha", "beta"]


def test_available_models_falls_back_on_failure() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="boom"))
    assert KiroAdapter(probe=probe).available_models() == []


def test_available_models_falls_back_on_bad_json() -> None:
    probe = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="not json"))
    assert KiroAdapter(probe=probe).available_models() == []
