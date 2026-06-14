# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for consensus over ACP: parallel fan-out, the caps, target parsing, and the tool/server wiring."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ConsensusRequest, Target
from rutherford.services.consensus import ConsensusService
from rutherford.services.delegation import DelegationService
from rutherford.tools.common import as_target, ensure_known_targets
from rutherford.tools.consensus import consensus_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))


def _service(config: RutherfordConfig | None = None) -> ConsensusService:
    resolved = config or RutherfordConfig()
    return ConsensusService(DelegationService(DescriptorRegistry([FAKE]), resolved), resolved)


def _app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))


async def test_consensus_collects_every_voice() -> None:
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake", model="m")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    result = await _service().consensus(request)
    assert len(result.voices) == 2
    assert all(voice.ok and "42" in voice.text for voice in result.voices)


async def test_consensus_requires_a_target() -> None:
    with pytest.raises(RutherfordError) as exc:
        await _service().consensus(ConsensusRequest(targets=[], prompt="x"))
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_consensus_enforces_target_cap() -> None:
    config = RutherfordConfig(max_targets=1)
    with pytest.raises(RutherfordError) as exc:
        await _service(config).consensus(ConsensusRequest(targets=[Target(cli="fake"), Target(cli="fake")], prompt="x"))
    assert exc.value.code is ErrorCode.TOO_MANY_TARGETS


def test_as_target_and_known_targets() -> None:
    assert as_target("fake").cli == "fake"
    assert as_target("fake:m").model == "m"
    assert as_target({"cli": "fake", "model": "m"}).model == "m"
    assert as_target(Target(cli="fake")).cli == "fake"
    for bad in ({"model": "m"}, ":nope", 123):
        with pytest.raises(RutherfordError):
            as_target(bad)  # type: ignore[arg-type]
    registry = DescriptorRegistry([FAKE])
    ensure_known_targets(registry, [Target(cli="fake")])
    with pytest.raises(RutherfordError):
        ensure_known_targets(registry, [Target(cli="nope")])


async def test_consensus_tool_and_server_wrapper(monkeypatch: Any) -> None:
    # The nested voices array round-trips poorly through python-toon's decoder, so assert on the encoded
    # text (the production output): both voices answered, so "42" appears twice.
    out = await consensus_tool(
        _app(), prompt="what is 17 + 25?", targets=["fake", "fake:m"], working_dir=str(REPO_ROOT)
    )
    assert out.count("42") == 2
    monkeypatch.setattr(server, "_APP", _app())
    wrapped = await server.consensus(prompt="what is 17 + 25?", targets=["fake"], working_dir=str(REPO_ROOT))
    assert "42" in wrapped
