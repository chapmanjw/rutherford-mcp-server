# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the OpenCode adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.opencode import OpenCodeAdapter
from rutherford.domain.enums import AuthState, SafetyMode, Stance
from rutherford.domain.models import (
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Target,
)
from rutherford.services.strategies import apply_stance
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "opencode"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="opencode", model="anthropic/claude-sonnet-4-6"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {
        "target": Target(cli="opencode", model="anthropic/claude-sonnet-4-6"),
        "prompt": "say hi",
    }
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


# --- build_invocation --------------------------------------------------------


def test_build_invocation_prompt_goes_on_stdin() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(), _ctx())
    assert isinstance(spec.argv, list)
    assert spec.argv[:4] == ["opencode", "run", "--format", "json"]
    # The prompt rides on stdin, not as a positional argv element. OpenCode launches via a
    # cmd.exe shim on Windows, where a newline in an argv element truncates the command; stdin
    # carries the full prompt intact.
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv
    assert "-m" in spec.argv
    assert spec.argv[spec.argv.index("-m") + 1] == "anthropic/claude-sonnet-4-6"


def test_build_invocation_includes_working_dir_and_resume() -> None:
    spec = OpenCodeAdapter().build_invocation(
        _req(working_dir="/work", session_id="ses_1"),
        _ctx(),
    )
    assert "--dir" in spec.argv
    assert spec.argv[spec.argv.index("--dir") + 1] == "/work"
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("--session") + 1] == "ses_1"
    assert spec.stdin == "say hi"


def test_build_invocation_prepends_role_preamble_to_prompt() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(), _ctx(preamble="You are a reviewer."))
    # No system-prompt flag exists; the preamble is folded into the stdin prompt.
    assert "--system-prompt" not in spec.argv
    assert "--append-system-prompt" not in spec.argv
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a reviewer.")
    assert "say hi" in spec.stdin


def test_build_invocation_appends_files_to_prompt() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(files=["a.py", "b.py"]), _ctx())
    assert spec.stdin is not None
    assert "Files in scope:" in spec.stdin
    assert "- a.py" in spec.stdin


def test_build_invocation_never_builds_a_shell_string() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(prompt="rm -rf / ; echo pwned"), _ctx())
    # The dangerous text rides on stdin, never an argv element / command line.
    assert spec.stdin == "rm -rf / ; echo pwned"
    assert all(";" not in arg for arg in spec.argv)


def test_multiline_stance_prompt_survives_intact_on_stdin() -> None:
    # Regression: a consensus stance directive joined to the claim with a blank line must reach
    # OpenCode as one coherent stdin message. Passing it as a positional argv element silently
    # dropped everything after the first newline through the Windows cmd.exe shim, so OpenCode
    # saw only the stance directive and asked for the proposition.
    claim = 'CLAIM TO DEBATE: "A message queue is overkill for a single-server app."'
    steered = apply_stance(claim, Stance.AGAINST)
    assert "\n" in steered  # the directive and claim are separated by a blank line

    spec = OpenCodeAdapter().build_invocation(_req(prompt=steered), _ctx())

    assert spec.stdin == steered  # the full steered prompt, intact
    assert "Argue against" in spec.stdin  # the stance directive
    assert claim in spec.stdin  # AND the full claim body
    # The claim must not be sitting in an argv element where the shim would truncate it.
    assert all(claim not in arg for arg in spec.argv)
    assert all("\n" not in arg for arg in spec.argv)


# --- map_safety --------------------------------------------------------------


def test_map_safety_covers_every_mode() -> None:
    adapter = OpenCodeAdapter()
    flags = {mode: adapter.map_safety(mode) for mode in SafetyMode}
    deny = '{"edit":"deny","bash":"deny"}'
    allow = '{"edit":"allow","bash":"allow"}'
    assert flags[SafetyMode.READ_ONLY].env == {"OPENCODE_PERMISSION": deny}
    assert flags[SafetyMode.READ_ONLY].args == []
    assert flags[SafetyMode.PROPOSE].env == {"OPENCODE_PERMISSION": deny}
    assert flags[SafetyMode.PROPOSE].args == []
    assert flags[SafetyMode.WRITE].env == {"OPENCODE_PERMISSION": allow}
    assert flags[SafetyMode.WRITE].args == []
    assert flags[SafetyMode.YOLO].args == ["--dangerously-skip-permissions"]


def test_build_invocation_overlays_safety_env() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.WRITE))
    assert spec.env.get("OPENCODE_PERMISSION") == '{"edit":"allow","bash":"allow"}'


def test_build_invocation_yolo_adds_skip_permissions_flag() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.YOLO))
    assert "--dangerously-skip-permissions" in spec.argv
    # The prompt is on stdin, not in argv.
    assert spec.stdin == "say hi"


def test_build_invocation_read_only_denies_in_env() -> None:
    spec = OpenCodeAdapter().build_invocation(_req(), _ctx(safety=SafetyMode.READ_ONLY))
    assert spec.env.get("OPENCODE_PERMISSION") == '{"edit":"deny","bash":"deny"}'
    assert "--dangerously-skip-permissions" not in spec.argv


# --- parse_output (golden) ---------------------------------------------------


def test_parse_success_golden() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=1.9)
    result = OpenCodeAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "ses_7c1a9b3e2d4f5a6b"
    assert result.cost is not None
    assert result.cost.usd == 0.0098
    assert result.cost.input_tokens == 1180
    assert result.cost.output_tokens == 12


def test_parse_error_stream_with_nonzero_exit_golden() -> None:
    raw = ProcessResult(exit_code=1, stdout=_sample("error.jsonl"), stderr="auth error", duration_s=0.6)
    result = OpenCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


def test_parse_nonzero_exit_with_no_text() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr="opencode: command failed", duration_s=0.4)
    result = OpenCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "command failed" in result.error.message


def test_parse_garbage_stdout_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout="not json at all", duration_s=0.1)
    result = OpenCodeAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_nonzero_exit_with_answer_is_success() -> None:
    # parse_output defers to the shared finalize_answer, so a non-zero exit with a parsed answer
    # and no in-band failure is still the answer (a CLI can exit non-zero on a permission denial
    # yet have produced a valid answer) -- aligned with codex in the same panel.
    line = '{"type":"text","sessionID":"ses_x1","part":{"id":"p1","text":"the answer"}}'
    raw = ProcessResult(exit_code=1, stdout=line, stderr="exited after answering", duration_s=0.3)
    result = OpenCodeAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "the answer"
    assert result.session_id == "ses_x1"


# --- detect / check_auth / available_models ----------------------------------


def test_check_auth_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    status = OpenCodeAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    status = OpenCodeAdapter(probe=FakeProbe()).check_auth()
    assert status.state is AuthState.AUTHENTICATED


def test_check_auth_with_persisted_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=0, stdout="anthropic\nopenai"))
    assert OpenCodeAdapter(probe=probe).check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_needs_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="no credentials"))
    assert OpenCodeAdapter(probe=probe).check_auth().state is AuthState.NEEDS_LOGIN


def test_available_models_queries_cli() -> None:
    probe = FakeProbe(
        run_fn=lambda argv: ProcessResult(
            exit_code=0,
            stdout="anthropic/claude-sonnet-4-6\nanthropic/claude-opus-4-6\nopenai/gpt-5\n",
        )
    )
    models = OpenCodeAdapter(probe=probe).available_models()
    assert models == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-6",
        "openai/gpt-5",
    ]


def test_available_models_falls_back_on_failure() -> None:
    probe = FakeProbe(default_result=ProcessResult(exit_code=1, stderr="boom"))
    assert OpenCodeAdapter(probe=probe).available_models() == []


def test_parse_cumulative_snapshot_stream_returns_the_latest_snapshot() -> None:
    # The full-codebase panel's MAJOR: a stream whose text events carry cumulative SNAPSHOTS
    # (each repeating and extending the last) used to be concatenated -- "H"+"He"+"Hel" -- and the
    # old joined.count(longest) > 1 guard never fired because the longest snapshot appears exactly
    # once in that concatenation. The prefix chain must be detected and the latest snapshot win.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"The answer"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"The answer is"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"The answer is 42."}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "The answer is 42."


def test_parse_delta_stream_with_repeated_chunk_concatenates_fully() -> None:
    # A legitimate delta stream may repeat a chunk (a repeated line in the answer). The old
    # duplicated-longest guard collapsed the whole stream to that one chunk; the prefix rule
    # only short-circuits when every chunk is a prefix of the longest, so deltas concatenate.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"abc"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"x"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"abc"}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "abcxabc"


def test_parse_repeated_tail_snapshot_resolves_to_the_tail() -> None:
    # Snapshots whose final state is emitted twice fail the strict-increase chain, but every
    # chunk is a prefix of the longest, so the prefix rule resolves to that final snapshot.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hello"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hello world"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hello world"}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "Hello world"


def test_parse_delta_stream_still_concatenates() -> None:
    # True deltas (no prefix chain) keep concatenating -- the snapshot heuristic must not eat them.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hel"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"lo "}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"world"}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "Hello world"


def test_parse_multi_part_cumulative_snapshots_resolve_per_part() -> None:
    # Round-1 review blocker: two INTERLEAVED cumulative streams (distinct part ids) do not form
    # one prefix chain, so a global-chain check fell back to concatenation and returned doubled
    # text ("HelloHello worldworld!"). Streams must resolve per part id, then join in order.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hello"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p2","text":"world"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hello "}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p2","text":"world!"}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "Hello world!"


def test_parse_multi_part_delta_streams_concatenate_per_part() -> None:
    # Interleaved DELTA streams keep concatenating within each part, joined in part order.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"Hel"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p2","text":" Wor"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"lo"}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p2","text":"ld"}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "Hello World"


def test_parse_repeated_identical_snapshot_resolves_to_one_copy() -> None:
    # The equal-snapshot guard: a stream that emits the same full text twice (a final snapshot
    # repeated) fails the strict-increase chain but is caught by the duplicated-longest check.
    # NOTE the deliberate ambiguity this pins: identical repeated chunks ("Hel","Hel") could in
    # principle be deltas meaning "HelHel", but on a snapshot-prone stream the repeated-snapshot
    # reading is the safe one -- this test is the recorded decision.
    lines = "\n".join(
        [
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"The answer is 42."}}',
            '{"type":"text","sessionID":"s1","part":{"id":"p1","text":"The answer is 42."}}',
        ]
    )
    result = OpenCodeAdapter().parse_output(ProcessResult(exit_code=0, stdout=lines, duration_s=0.2), _ctx())
    assert result.ok
    assert result.text == "The answer is 42."
