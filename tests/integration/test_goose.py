# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration tests: drive the real ``goose acp`` agent over ACP (local only, run with -m integration).

These verify the full ACP-native stack -- delegate, consensus, and debate (persistent sessions) -- against
a real agent, not the fake one. Slow (real model calls); deselected by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.acp.descriptors import default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import ConsensusRequest, DebateRequest, Target
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService

pytestmark = pytest.mark.integration

_PROMPT = "Reply with ONLY the number, nothing else: what is 17 + 25?"


async def test_goose_delegate_turn() -> None:
    goose = default_registry().get("goose")
    result = await run_acp_turn(
        goose, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=120.0
    )
    assert result.ok is True, f"goose failed: {result.error}"
    assert "42" in result.text
    assert result.session_id is not None


@pytest.mark.parametrize("agent_id", ["goose", "vibe", "junie", "opencode"])
async def test_working_agent_answers(agent_id: str) -> None:
    """The agents that drive cleanly over ACP-stdio on this machine each answer a trivial prompt."""
    descriptor = default_registry().get(agent_id)
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=120.0
    )
    assert result.ok is True, f"{agent_id} failed: {result.error}"
    assert "42" in result.text


@pytest.mark.parametrize("agent_id", ["codex", "claude_code"])
async def test_official_adapter_answers(agent_id: str) -> None:
    """The official Zed adapters drive their CLI over ACP using the existing CLI login (no API key).

    ``codex`` (codex-acp) reuses the ChatGPT login and ``claude_code`` (claude-agent-acp) reuses the Claude
    Code login; both stream an answer end to end (receipt 11-official-adapters-auth-test.md). A longer budget
    than the other agents because the first turn also negotiates the underlying CLI's auth.
    """
    descriptor = default_registry().get(agent_id)
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=180.0
    )
    assert result.ok is True, f"{agent_id} failed: {result.error}"
    assert "42" in result.text


@pytest.mark.parametrize("agent_id", ["copilot", "qwen", "droid", "cursor", "kiro", "pi"])
async def test_second_wave_agent_answers(agent_id: str) -> None:
    """The second/third wave (probed live, receipts 12/13) each drive over ACP with the existing CLI auth.

    copilot (GitHub Copilot plan), qwen (~/.qwen), droid (Factory -- separate billing), cursor (Cursor
    subscription; the `acp` subcommand is hidden from --help), kiro (kiro-cli, not the IDE-launcher `kiro`),
    pi (the pi-acp wrapper over `pi --mode rpc`). Each answers a trivial prompt end to end.

    Not parametrized here, on purpose:
    - hermes: registered and functions over ACP (probe answers in ~7-9s), but the Nous endpoint latency
      swings from seconds to >190s, so it cannot satisfy a bounded-timeout assertion -- check it with
      ``doctor`` live instead.
    - kilo: its Auto Kilo Free Gateway works only in the interactive TUI, not a headless spawn; it needs a
      real ``kilo auth`` credential before a headless turn completes.
    """
    descriptor = default_registry().get(agent_id)
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=180.0
    )
    assert result.ok is True, f"{agent_id} failed: {result.error}"
    assert "42" in result.text


async def test_goose_consensus_two_voices() -> None:
    config = RutherfordConfig()
    service = ConsensusService(DelegationService(default_registry(), config), config)
    request = ConsensusRequest(
        targets=[Target(cli="goose"), Target(cli="goose")], prompt=_PROMPT, working_dir=str(Path.cwd()), timeout_s=120.0
    )
    result = await service.consensus(request)
    assert len(result.voices) == 2
    assert any(voice.ok for voice in result.voices), f"all voices failed: {[v.error for v in result.voices]}"
    assert all("42" in voice.text for voice in result.voices if voice.ok)


async def test_goose_debate_persistent_sessions() -> None:
    config = RutherfordConfig()
    service = DebateService(default_registry(), config)
    request = DebateRequest(
        targets=[Target(cli="goose"), Target(cli="goose")],
        prompt=_PROMPT,
        rounds=2,
        working_dir=str(Path.cwd()),
        timeout_s=120.0,
    )
    result = await service.debate(request)
    assert len(result.rounds) >= 1
    assert any(contribution.ok for round_ in result.rounds for contribution in round_.contributions)
