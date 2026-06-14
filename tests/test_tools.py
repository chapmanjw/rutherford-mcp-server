# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the thin layers: common parsers, context helpers, the tools, and the FastMCP server."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context, error_payload_from, tool_error, tool_success
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import decode
from rutherford.tools.capabilities import capabilities_tool
from rutherford.tools.common import ensure_known_agent, parse_safety_mode, resolve_safety_mode
from rutherford.tools.delegate import delegate_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))


def _app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))


def test_common_parsers() -> None:
    assert parse_safety_mode("write") is SafetyMode.WRITE
    assert parse_safety_mode(SafetyMode.YOLO) is SafetyMode.YOLO
    assert resolve_safety_mode(None, SafetyMode.READ_ONLY) is SafetyMode.READ_ONLY
    assert resolve_safety_mode("yolo", SafetyMode.READ_ONLY) is SafetyMode.YOLO
    with pytest.raises(RutherfordError):
        parse_safety_mode("bogus")
    registry = DescriptorRegistry([FAKE])
    ensure_known_agent(registry, "fake")
    with pytest.raises(RutherfordError):
        ensure_known_agent(registry, "nope")


def test_envelope_helpers() -> None:
    assert "1" in tool_success({"a": 1})
    error = decode(tool_error(ErrorCode.INTERNAL, "boom", {"k": "v"}))
    assert error["error"]["code"] == "INTERNAL" and error["error"]["details"]["k"] == "v"
    assert "INVALID_INPUT" in error_payload_from(RutherfordError(ErrorCode.INVALID_INPUT, "bad"))


async def test_capabilities_tool_lists_agents() -> None:
    data = decode(await capabilities_tool(_app()))
    assert any(agent["id"] == "fake" for agent in data["agents"])


async def test_delegate_tool_ok_and_unknown() -> None:
    out = await delegate_tool(_app(), cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    assert "42" in out
    with pytest.raises(RutherfordError):
        await delegate_tool(_app(), cli="nope", prompt="x")


async def test_server_guarded_paths() -> None:
    async def ok() -> str:
        return "fine"

    assert await server._guarded(ok()) == "fine"

    async def rutherford_error() -> str:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "no")

    with pytest.raises(ToolError):
        await server._guarded(rutherford_error())

    async def crash() -> str:
        raise ValueError("boom")

    with pytest.raises(ToolError):
        await server._guarded(crash())


async def test_server_tool_wrappers(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", _app())
    out = await server.delegate(cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    assert "42" in out
    caps = await server.capabilities()
    assert "fake" in caps


def test_get_app_and_main(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", None)
    app = server.get_app()
    assert app is not None
    assert server.get_app() is app  # cached on the second call
    monkeypatch.setattr(server, "_APP", None)
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: None)
    server.main()
    assert server._APP is not None
