# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The MCP wire contract: a real initialize / tools\\_list / tools\\_call round trip against the server.

Everything else in the suite calls the tool functions directly, and ``--smoke`` returns from ``main()``
*before* ``mcp.run()``. So nothing exercised the protocol layer itself -- the one path an ``mcp`` or
``fastmcp`` version bump can break while every other test stays green.

This uses FastMCP's in-memory client transport: no subprocess, no socket, no stdio pipe, but the same
``initialize`` handshake, schema generation, and result envelopes a real client drives. It belongs in
the default suite rather than behind the ``integration`` marker for exactly that reason -- it needs
nothing installed and finishes in milliseconds.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import build_app_context

FAKE = AgentDescriptor("fake", "Fake", ("fake-acp",))

#: Tools whose absence would mean the surface silently shrank. Not the full roster on purpose: pinning
#: every name would turn "we added a tool" into a failing test.
_CORE_TOOLS = {"delegate", "consensus", "debate", "review", "plan", "doctor", "capabilities", "setup"}


@pytest.fixture(autouse=True)
def _app(monkeypatch: Any) -> None:
    """Point the server at a fake single-agent roster so no real CLI is ever spawned."""
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))
    monkeypatch.setattr(server, "_APP", app)


async def test_initialize_and_list_tools() -> None:
    """The handshake completes and the tool surface is advertised with usable schemas."""
    async with Client(server.mcp) as client:
        tools = await client.list_tools()

    by_name = {t.name: t for t in tools}
    missing = _CORE_TOOLS - set(by_name)
    assert not missing, f"tools missing from the MCP surface: {sorted(missing)}"

    # A tool the client cannot describe is a tool the model cannot call correctly.
    delegate = by_name["delegate"]
    assert delegate.description
    assert delegate.inputSchema["type"] == "object"
    assert "cli" in delegate.inputSchema["properties"]
    assert "prompt" in delegate.inputSchema["properties"]


async def test_tools_call_returns_a_decodable_result() -> None:
    """A real tools/call round trip: request over the protocol, result back through the envelope."""
    async with Client(server.mcp) as client:
        result = await client.call_tool("capabilities", {})

    text = "".join(block.text for block in result.content if getattr(block, "type", "") == "text")
    assert "fake" in text  # the configured roster came back through the wire, not a direct call


async def test_a_tool_error_surfaces_as_a_protocol_error() -> None:
    """A refused call must come back as an MCP error, not a success carrying an error payload.

    This is the half of the contract a direct function call cannot check: the exception has to survive
    translation into the wire envelope.
    """
    async with Client(server.mcp) as client:
        with pytest.raises(ToolError) as exc:
            await client.call_tool("delegate", {"cli": "does-not-exist", "prompt": "hi"})

    assert "does-not-exist" in str(exc.value) or "UNKNOWN_TARGET" in str(exc.value)
