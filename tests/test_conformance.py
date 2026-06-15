# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for ACP conformance probing and the doctor tool."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.conformance import classify, probe_agent
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import build_app_context
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationResult, ErrorInfo, Target
from rutherford.io.serialize import decode
from rutherford.tools import capabilities as capabilities_module
from rutherford.tools.capabilities import _LOCAL_PROBE_TIMEOUT_S, _probe_timeout, doctor_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))
DEAD = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
BAD = AgentDescriptor("bad", "Bad", ("this-binary-does-not-exist-xyz123",))
OLLAMA = AgentDescriptor("local", "Local", ("local-acp",), provider="ollama")
LOCALHOST = AgentDescriptor(
    "host", "Host", ("host-acp",), env_overrides=(("OPENAI_BASE_URL", "http://localhost:1234"),)
)
LOCALHOST_UPPER = AgentDescriptor(
    "hostu", "HostU", ("hostu-acp",), env_overrides=(("OPENAI_BASE_URL", "http://LOCALHOST:1234/v1"),)
)


def _result(ok: bool, code: ErrorCode | None = None) -> DelegationResult:
    error = ErrorInfo(code=code, message="m") if code is not None else None
    return DelegationResult(target=Target(cli="x"), ok=ok, error=error, text="OK" if ok else "")


def test_classify_covers_every_outcome() -> None:
    assert classify("x", _result(True)).status == "ok"
    assert classify("x", _result(False, ErrorCode.ACP_SPAWN_FAILED)).status == "not_installed"
    assert classify("x", _result(False, ErrorCode.ACP_HANDSHAKE_FAILED)).status == "handshake_failed"
    assert classify("x", _result(False, ErrorCode.ACP_EMPTY_ANSWER)).status == "no_answer"
    assert classify("x", _result(False, ErrorCode.ACP_REFUSED)).status == "no_answer"
    assert classify("x", _result(False, ErrorCode.ACP_TURN_ERROR)).status == "error"
    assert classify("x", _result(False, None)).status == "error"
    assert classify("x", _result(False, ErrorCode.ACP_SPAWN_FAILED)).installed is False


async def test_probe_agent_working() -> None:
    report = await probe_agent(FAKE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "ok" and report.installed and report.answered


async def test_probe_agent_not_installed() -> None:
    report = await probe_agent(BAD, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert report.status == "not_installed" and report.installed is False


async def test_probe_agent_handshake_failed() -> None:
    report = await probe_agent(DEAD, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert report.status == "handshake_failed" and report.installed is True


async def test_doctor_tool_probes_roster(monkeypatch: Any) -> None:
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE, BAD]))
    data = decode(await doctor_tool(app, timeout_s=30.0))
    assert len(data["agents"]) == 2
    one = decode(await doctor_tool(app, agent="fake", timeout_s=30.0))
    assert len(one["agents"]) == 1 and one["agents"][0]["agent_id"] == "fake"
    monkeypatch.setattr(server, "_APP", app)
    wrapped = await server.doctor(agent="fake", timeout_s=30.0)
    assert "fake" in wrapped


async def test_doctor_unknown_agent() -> None:
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))
    with pytest.raises(RutherfordError):
        await doctor_tool(app, agent="nope")


def test_probe_timeout_floors_local_models() -> None:
    # A cloud agent keeps the call default; a local-runtime provider gets the cold-start floor.
    assert _probe_timeout(FAKE, 60.0) == 60.0
    assert _probe_timeout(OLLAMA, 60.0) == _LOCAL_PROBE_TIMEOUT_S
    # A configured backend agent pointing at a localhost endpoint is local too, even with no provider set.
    assert _probe_timeout(LOCALHOST, 60.0) == _LOCAL_PROBE_TIMEOUT_S
    # URL hosts are case-insensitive: an uppercase LOCALHOST is still local.
    assert _probe_timeout(LOCALHOST_UPPER, 60.0) == _LOCAL_PROBE_TIMEOUT_S


def test_probe_timeout_is_a_floor_not_an_override() -> None:
    # An explicit budget larger than the floor still wins -- the floor only RAISES a too-short default.
    assert _probe_timeout(OLLAMA, _LOCAL_PROBE_TIMEOUT_S + 120.0) == _LOCAL_PROBE_TIMEOUT_S + 120.0
    assert _probe_timeout(FAKE, 600.0) == 600.0


async def test_doctor_budgets_a_cold_local_model(monkeypatch: Any) -> None:
    # The doctor tool must hand a local-model agent the generous floor, not the 60s cloud default that
    # false-flags a cold model loading on its first prompt. Capture the timeout each probe actually receives.
    seen: dict[str, float] = {}

    async def _fake_probe(descriptor: AgentDescriptor, *, timeout_s: float = 60.0) -> Any:
        seen[descriptor.id] = timeout_s
        return classify(descriptor.id, _result(True))

    monkeypatch.setattr(capabilities_module, "probe_agent", _fake_probe)
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE, OLLAMA]))
    await doctor_tool(app, timeout_s=60.0)
    assert seen["fake"] == 60.0
    assert seen["local"] == _LOCAL_PROBE_TIMEOUT_S
