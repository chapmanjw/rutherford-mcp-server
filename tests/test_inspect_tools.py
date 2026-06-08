# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the capabilities, doctor, and list_roles tools, plus the server smoke path."""

from __future__ import annotations

import pytest

import rutherford.server as server
from rutherford.adapters.claude_code import ClaudeCodeAdapter
from rutherford.config.schema import AdapterConfig, RutherfordConfig
from rutherford.domain.enums import AuthState
from rutherford.domain.models import ProcessResult
from rutherford.tools.capabilities import capabilities_tool, doctor_tool
from rutherford.tools.roles import list_roles_tool
from tests.fakes import FakeAdapter, FakeProbe, FakeProcessRunner, make_app


async def test_capabilities_lists_each_adapter() -> None:
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b", installed=False)])
    out = await capabilities_tool(app)
    assert "id: a" in out
    assert "id: b" in out
    assert "installed: false" in out  # b is not installed


async def test_doctor_diagnoses_uninstalled_adapter() -> None:
    app = make_app(adapters=[FakeAdapter("b", installed=False)])
    out = await doctor_tool(app)
    assert "not found on PATH" in out
    assert "max_depth" in out


async def test_capabilities_marks_optional_adapter() -> None:
    app = make_app(adapters=[FakeAdapter("ollama", optional=True), FakeAdapter("a")])
    out = await capabilities_tool(app)
    assert "optional: true" in out


async def test_doctor_frames_absent_optional_adapter_as_optional_not_an_error() -> None:
    app = make_app(adapters=[FakeAdapter("ollama", installed=False, optional=True)])
    out = await doctor_tool(app)
    # An absent optional adapter reads as "only if you want it", never as something to fix.
    assert "optional" in out
    assert "only if you want local delegation" in out
    assert "was not found on PATH" not in out


async def test_capabilities_shows_the_configured_default_model() -> None:
    app = make_app(
        adapters=[FakeAdapter("ollama")],
        config=RutherfordConfig(adapters={"ollama": AdapterConfig(default_model="qwen2.5-coder")}),
    )
    out = await capabilities_tool(app)
    assert "default_model" in out and "qwen2.5-coder" in out


async def test_doctor_verifies_unknown_auth_by_default() -> None:
    app = make_app(
        adapters=[FakeAdapter("a", auth_state=AuthState.UNKNOWN)],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    # Default doctor verifies an unprobeable adapter with a real round trip and reclassifies it.
    out = await doctor_tool(app)
    assert "authenticated" in out
    assert "verified by a live round trip" in out


async def test_doctor_live_false_skips_verification() -> None:
    app = make_app(
        adapters=[FakeAdapter("a", auth_state=AuthState.UNKNOWN)],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    # live=False is the metadata-only path: no model call, so the state stays unknown.
    out = await doctor_tool(app, live=False)
    assert "unknown" in out


async def test_doctor_marks_failed_unknown_as_needs_login() -> None:
    app = make_app(
        adapters=[FakeAdapter("a", auth_state=AuthState.UNKNOWN)],
        runner=FakeProcessRunner(ProcessResult(exit_code=1, stderr="no credentials")),
    )
    out = await doctor_tool(app)
    assert "needs_login" in out


async def test_doctor_promotes_bedrock_claude_to_authenticated_via_live(monkeypatch: pytest.MonkeyPatch) -> None:
    # End to end: a Bedrock-configured Claude Code reports `apiProvider: bedrock` from `auth status`,
    # so check_auth returns UNKNOWN; doctor's live round trip then confirms it AUTHENTICATED.
    for var in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bedrock_status = '{"loggedIn": true, "authMethod": "third_party", "apiProvider": "bedrock"}'

    def probe_run(argv: list[str]) -> ProcessResult:
        if "auth" in argv:
            return ProcessResult(exit_code=0, stdout=bedrock_status)
        return ProcessResult(exit_code=0, stdout="2.1.158 (Claude Code)")  # --version

    adapter = ClaudeCodeAdapter(probe=FakeProbe(which_map={"claude": "/usr/bin/claude"}, run_fn=probe_run))
    # The live test prompt returns a valid claude JSON result envelope -> the round trip succeeds.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout='{"result": "ok", "session_id": "s1"}'))
    app = make_app(adapters=[adapter], runner=runner)

    out = await doctor_tool(app)
    assert "authenticated" in out
    assert "confirmed by a live invocation" in out


async def test_doctor_live_false_leaves_bedrock_claude_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cheap path stays honest and spends no model call: a Bedrock claude_code reads as `unknown`.
    for var in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def probe_run(argv: list[str]) -> ProcessResult:
        if "auth" in argv:
            return ProcessResult(exit_code=0, stdout='{"loggedIn": true, "apiProvider": "bedrock"}')
        return ProcessResult(exit_code=0, stdout="2.1.158 (Claude Code)")

    adapter = ClaudeCodeAdapter(probe=FakeProbe(which_map={"claude": "/usr/bin/claude"}, run_fn=probe_run))
    app = make_app(adapters=[adapter], runner=FakeProcessRunner())
    out = await doctor_tool(app, live=False)
    assert "unknown" in out


async def test_list_roles_includes_builtins() -> None:
    app = make_app(adapters=[FakeAdapter("a")])
    out = await list_roles_tool(app)
    assert "planner" in out
    assert "codereviewer" in out


def test_server_smoke_prints_ready(capsys: pytest.CaptureFixture[str]) -> None:
    server._smoke()
    captured = capsys.readouterr()
    assert "ready with" in captured.out
    assert "claude_code" in captured.out
