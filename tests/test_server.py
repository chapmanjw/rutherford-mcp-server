# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the FastMCP server wrappers and the error-mapping guard."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastmcp.exceptions import ToolError

import rutherford.server as server
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ProcessResult, Target
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app


@pytest.fixture
def wired_server() -> Iterator[None]:
    """Point the server's shared app at a fake-backed context for the duration of a test."""
    previous = server._APP
    server._APP = make_app(
        adapters=[FakeAdapter("fake"), FakeAdapter("other")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="answer")),
    )
    try:
        yield
    finally:
        server._APP = previous


async def test_all_tools_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {getattr(tool, "name", tool) for tool in tools}
    assert {"delegate", "consensus", "job_status", "job_result"} <= names


async def test_mcp_visible_safety_mode_schema() -> None:
    # The MCP-VISIBLE contract for the safety_mode parameter, pinned at the FastMCP layer (the
    # tool-function tests cannot see this surface): delegate/consensus/debate expose it as
    # optional-without-default-string (the None sentinel that lets config default_safety_mode
    # apply), review/plan do not expose it at all (clamped read_only), and setup keeps it with an
    # explicit read_only default (setup WRITES the config default, so None would be circular).
    schemas = {tool.name: tool.parameters for tool in await server.mcp.list_tools()}

    for name in ("delegate", "consensus", "debate"):
        properties = schemas[name]["properties"]
        assert "safety_mode" in properties, f"{name} lost its safety_mode parameter"
        assert "safety_mode" not in schemas[name].get("required", []), (
            f"{name}.safety_mode must stay optional (the omitted case is what config fills)"
        )
        assert properties["safety_mode"].get("default") != "read_only", (
            f"{name}.safety_mode must not advertise a read_only default -- a hardcoded string "
            "default would shadow the configured default_safety_mode"
        )

    for name in ("review", "plan"):
        assert "safety_mode" not in schemas[name]["properties"], (
            f"{name} is clamped to read_only; exposing safety_mode again would reopen the "
            "name-vs-behavior contract the clamp closed"
        )

    assert schemas["setup"]["properties"]["safety_mode"].get("default") == "read_only"


async def test_delegate_wrapper_returns_envelope(wired_server: None) -> None:
    out = await server.delegate(cli="fake", prompt="hello")
    assert "ok: true" in out
    assert "answer" in out


async def test_delegate_wrapper_bad_input_raises_tool_error(wired_server: None) -> None:
    with pytest.raises(ToolError) as info:
        await server.delegate(cli="fake", prompt="hello", safety_mode="nonsense")
    assert "INVALID_INPUT" in str(info.value)


async def test_consensus_wrapper_returns_voices(wired_server: None) -> None:
    out = await server.consensus(targets=[Target(cli="fake"), Target(cli="other")], prompt="q")
    assert "voices[2]" in out


async def test_debate_wrapper_returns_transcript(wired_server: None) -> None:
    out = await server.debate(prompt="q", targets=[Target(cli="fake"), Target(cli="other")], rounds=2)
    assert "rounds[2]" in out  # both rounds run when both voices survive
    assert "answer" in out


async def test_debate_wrapper_rejects_single_target(wired_server: None) -> None:
    with pytest.raises(ToolError, match="at least two targets"):
        await server.debate(prompt="q", targets=[Target(cli="fake")])


async def test_job_status_wrapper_unknown_raises_tool_error(wired_server: None) -> None:
    with pytest.raises(ToolError) as info:
        await server.job_status(job_id="missing")
    assert "JOB_NOT_FOUND" in str(info.value)


async def test_guarded_passthrough() -> None:
    async def ok() -> str:
        return "value"

    assert await server._guarded(ok()) == "value"


async def test_guarded_maps_rutherford_error() -> None:
    async def boom() -> str:
        raise RutherfordError("INVALID_INPUT", "bad input")

    with pytest.raises(ToolError) as info:
        await server._guarded(boom())
    assert "bad input" in str(info.value)


async def test_guarded_maps_unexpected_error() -> None:
    async def boom() -> str:
        raise ValueError("surprise")

    with pytest.raises(ToolError) as info:
        await server._guarded(boom())
    assert "INTERNAL" in str(info.value)


async def test_guarded_does_not_echo_exception_internals_to_the_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The exception text can carry filesystem paths or raw input; the client gets a fixed
    # message, while the traceback lands in the server-side log for the operator.
    async def boom() -> str:
        raise ValueError(r"secret detail C:\Users\someone\.tokens\cred")

    with pytest.raises(ToolError) as info, caplog.at_level("ERROR", logger="rutherford.server"):
        await server._guarded(boom())
    assert "secret detail" not in str(info.value)
    assert "internal server error" in str(info.value)
    assert any("unexpected error" in record.message for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)  # the traceback is in the log
