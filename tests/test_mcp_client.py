# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""End-to-end tests of the MCP layer via FastMCP's in-process client (no real CLI)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastmcp import Client

import rutherford.server as server
from rutherford.domain.models import ProcessResult
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app

EXPECTED_TOOLS = {
    "delegate",
    "consensus",
    "debate",
    "review",
    "plan",
    "capabilities",
    "doctor",
    "job_status",
    "job_result",
    "list_jobs",
    "cancel_job",
    "list_roles",
    "reload_panels",
    "setup",
}


@pytest.fixture
def wired_server() -> Iterator[None]:
    previous = server._APP
    server._APP = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="hello there")),
    )
    try:
        yield
    finally:
        server._APP = previous


async def test_mcp_lists_all_tools(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names == EXPECTED_TOOLS  # exact: a new/removed tool must update this set (and the docs)


async def test_mcp_delegate_round_trip(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("delegate", {"cli": "a", "prompt": "hi"})
        text = result.content[0].text
        assert "ok: true" in text
        assert "hello there" in text


async def test_mcp_debate_round_trip(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("debate", {"prompt": "q", "targets": ["a", "b"], "rounds": 1})
        text = result.content[0].text
        assert "rounds[1]" in text
        assert "hello there" in text


async def test_mcp_list_roles(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool("list_roles", {})
        assert "planner" in result.content[0].text


async def test_mcp_tool_error_surfaces(wired_server: None) -> None:
    async with Client(server.mcp) as client:
        with pytest.raises(Exception, match="INVALID_INPUT"):
            await client.call_tool("delegate", {"cli": "a", "prompt": "hi", "safety_mode": "bogus"})
