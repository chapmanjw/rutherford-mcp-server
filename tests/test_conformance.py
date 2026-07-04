# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for ACP conformance probing and the doctor tool."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.conformance import classify, probe_agent, probe_connection
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


def test_classify_model_unavailable_turn_error() -> None:
    # A turn that reached the prompt (spawn + handshake OK) but failed because the harness/provider rejected
    # the MODEL is reported distinctly -- the connection is healthy; only the model/provider config is wrong.
    # This is the Bedrock/Vertex case: doctor must NOT call the agent broken just because a model id was not
    # recognized.
    rejected = DelegationResult(
        target=Target(cli="x"),
        ok=False,
        error=ErrorInfo(code=ErrorCode.ACP_TURN_ERROR, message="the requested model is not available"),
        text="",
    )
    report = classify("x", rejected)
    assert report.status == "model_unavailable"
    assert report.installed is True and report.answered is False
    assert "model" in report.detail.lower()
    # The exact AWS Bedrock rejection a Claude Code seat hits (word order "model identifier is invalid")
    # must also classify as model_unavailable, not a generic broken-agent error.
    bedrock = DelegationResult(
        target=Target(cli="claude_code"),
        ok=False,
        error=ErrorInfo(
            code=ErrorCode.ACP_TURN_ERROR,
            message="Internal error: API Error (claude-opus-4-8): 400 The provided model identifier is invalid..",
        ),
        text="",
    )
    assert classify("claude_code", bedrock).status == "model_unavailable"
    # A generic turn error (no model-availability marker) still classifies as a plain "error".
    assert classify("x", _result(False, ErrorCode.ACP_TURN_ERROR)).status == "error"


_BEDROCK_CLAUDE = AgentDescriptor(
    "claude_code", "Claude Code", FAKE.command, provider="anthropic", underlying_cli="claude"
)


async def test_doctor_attaches_bedrock_remediation_hint(monkeypatch: Any) -> None:
    # A Bedrock Claude Code seat whose turn is rejected for its model id gets a targeted remediation hint
    # pointing at the per-agent [agents.<id>.env] fix -- not just a bare model_unavailable.
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_UNAVAILABLE", "1")
    report = await probe_agent(_BEDROCK_CLAUDE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "model_unavailable"
    assert report.remediation_hint is not None
    assert "[agents.claude_code.env]" in report.remediation_hint
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" in report.remediation_hint


async def test_doctor_no_remediation_hint_off_bedrock(monkeypatch: Any) -> None:
    # The same model rejection with no Bedrock indicator: still model_unavailable, but no Bedrock remediation.
    for var in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_UNAVAILABLE", "1")
    report = await probe_agent(_BEDROCK_CLAUDE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "model_unavailable" and report.remediation_hint is None


async def test_doctor_hint_sees_per_agent_env_overrides(monkeypatch: Any) -> None:
    # The Bedrock indicator can come from the seat's own [agents.<id>.env] (descriptor.env_overrides), not just
    # os.environ -- the hint reasons over the same env the subprocess gets.
    for var in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "RUTHERFORD_FAKE_MODEL_UNAVAILABLE"):
        monkeypatch.delenv(var, raising=False)
    seat = AgentDescriptor(
        "claude_code",
        "Claude Code",
        FAKE.command,
        provider="anthropic",
        underlying_cli="claude",
        env_overrides=(("CLAUDE_CODE_USE_BEDROCK", "1"), ("RUTHERFORD_FAKE_MODEL_UNAVAILABLE", "1")),
    )
    report = await probe_agent(seat, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "model_unavailable" and report.remediation_hint is not None


async def test_doctor_envelope_round_trips_the_multiline_remediation_hint(monkeypatch: Any) -> None:
    # The multi-line hint (a TOML snippet) must survive the TOON envelope serialization intact.
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_UNAVAILABLE", "1")
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([_BEDROCK_CLAUDE]))
    data = decode(await doctor_tool(app, timeout_s=60.0))
    agent = data["agents"][0]
    assert agent["status"] == "model_unavailable"
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" in agent["remediation_hint"]


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


async def test_probe_connection_reachable_with_models(monkeypatch: Any) -> None:
    # The handshake-only check: spawn + initialize + new_session succeed, capturing the session + models.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "model-a,model-b")
    report = await probe_connection(FAKE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "reachable" and report.connected is True and report.installed is True
    assert report.session_id == "fake-session-1"
    assert report.models == ["model-a", "model-b"]  # the "configure" signal -- what you can set --model to


async def test_probe_connection_reachable_without_models() -> None:
    report = await probe_connection(FAKE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "reachable" and report.connected is True and report.models == []


async def test_probe_connection_lists_config_option_models(monkeypatch: Any) -> None:
    # claude_code's adapter advertises its models on the configOptions "model" channel, not SessionModelState.
    # connect_only must surface them (it previously reported [] for such an agent -- the misleading case).
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet,haiku")
    report = await probe_connection(FAKE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "reachable" and report.models == ["default", "sonnet", "haiku"]


async def test_probe_connection_unions_both_model_channels(monkeypatch: Any) -> None:
    # When an agent advertises BOTH channels, available_models is the union in a deterministic order:
    # SessionModelState ids first, then config-option values not already present.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "m1,m2")
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "m2,sonnet")
    report = await probe_connection(FAKE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.models == ["m1", "m2", "sonnet"]


async def test_probe_agent_model_unavailable_is_not_a_broken_agent(monkeypatch: Any) -> None:
    # An agent that spawns + handshakes but whose turn fails because the harness/provider rejected the model
    # (the Bedrock/Vertex Claude Code case) is reported as "model_unavailable", NOT a broken "error" /
    # "handshake_failed". The ACP connection is healthy; only the model/provider config is wrong.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_UNAVAILABLE", "1")
    report = await probe_agent(FAKE, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert report.status == "model_unavailable"
    assert report.installed is True and report.answered is False


async def test_probe_connection_handshake_failed() -> None:
    report = await probe_connection(DEAD, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert report.status == "handshake_failed" and report.installed is True and report.connected is False


async def test_probe_connection_not_installed() -> None:
    report = await probe_connection(BAD, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert report.status == "not_installed" and report.installed is False and report.connected is False


async def test_probe_connection_unexpected_error_is_a_clean_report(monkeypatch: Any) -> None:
    async def _boom(_self: Any) -> None:
        raise ValueError("kaboom")

    monkeypatch.setattr("rutherford.acp.session.ACPSession.open", _boom)
    report = await probe_connection(FAKE, cwd=str(REPO_ROOT), timeout_s=10.0)
    # An unexpected open() fault is a structured `error` report (never raised out of the probe), and the
    # session is torn down -- no leak.
    assert report.status == "error" and report.connected is False and "kaboom" in report.detail


async def test_probe_connection_cleans_up_then_propagates_cancellation(monkeypatch: Any) -> None:
    import asyncio

    closed: list[bool] = []

    async def _cancel(_self: Any) -> None:
        raise asyncio.CancelledError

    async def _record_close(_self: Any) -> None:
        closed.append(True)

    monkeypatch.setattr("rutherford.acp.session.ACPSession.open", _cancel)
    monkeypatch.setattr("rutherford.acp.session.ACPSession.close", _record_close)
    with pytest.raises(asyncio.CancelledError):
        await probe_connection(FAKE, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert closed  # cancellation tore the session down before propagating (never leaks the spawned process)


async def test_doctor_connect_only_reports_reachable(monkeypatch: Any) -> None:
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "model-x")
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE, BAD]))
    # The connect_only reports are a uniform array, so the envelope round-trips through the TOON seam.
    data = decode(await doctor_tool(app, connect_only=True, timeout_s=30.0))
    by_id = {agent["agent_id"]: agent for agent in data["agents"]}
    assert by_id["fake"]["status"] == "reachable" and by_id["fake"]["connected"] is True
    assert by_id["fake"]["session_id"] == "fake-session-1" and by_id["fake"]["models"] == ["model-x"]
    assert by_id["bad"]["status"] == "not_installed" and by_id["bad"]["connected"] is False
    monkeypatch.setattr(server, "_APP", app)
    wrapped = await server.doctor(connect_only=True, timeout_s=30.0)
    assert "reachable" in wrapped


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
