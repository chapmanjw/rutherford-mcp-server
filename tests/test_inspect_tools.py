# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the capabilities, doctor, and list_roles tools, plus the server smoke path."""

from __future__ import annotations

import pytest

import rutherford.server as server
from rutherford.tools.capabilities import capabilities_tool, doctor_tool
from rutherford.tools.roles import list_roles_tool
from tests.fakes import FakeAdapter, make_app


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
