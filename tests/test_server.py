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
