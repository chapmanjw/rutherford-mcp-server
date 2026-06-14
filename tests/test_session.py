# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for driving a turn (run_acp_turn) and the delegation service, against the fake ACP agent."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.journal import EventJournal, JournalEvent
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPHandshakeError, ACPSession, _post_prompt_safety, run_acp_turn
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import ReexecutionSafety, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationRequest, DelegationResult, Target
from rutherford.services.delegation import DelegationService

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))
_READ_ONLY = PermissionPolicy(SafetyMode.READ_ONLY)


async def _turn(prompt: str, *, timeout_s: float = 60.0, descriptor: AgentDescriptor = FAKE) -> DelegationResult:
    return await run_acp_turn(descriptor, prompt, policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=timeout_s)


async def test_run_turn_normal_answer() -> None:
    result = await _turn("what is 17 + 25?")
    assert result.ok is True and "42" in result.text
    assert result.session_id == "fake-session-1"
    assert result.provenance is not None
    assert result.safety_mode is SafetyMode.READ_ONLY


async def test_run_turn_refusal() -> None:
    result = await _turn("REFUSE this request")
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.ACP_REFUSED
    assert result.error.reexecution_safety is ReexecutionSafety.DUPLICATE_COST


async def test_run_turn_empty_answer() -> None:
    result = await _turn("EMPTY answer please")
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_EMPTY_ANSWER


async def test_run_turn_timeout() -> None:
    result = await _turn("HANG forever", timeout_s=1.0)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_TURN_TIMEOUT
    assert result.error.reexecution_safety is ReexecutionSafety.DUPLICATE_COST


async def test_run_turn_spawn_failure() -> None:
    bad = AgentDescriptor("bad", "Bad", ("this-binary-does-not-exist-xyz123",))
    result = await _turn("hi", descriptor=bad)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_SPAWN_FAILED
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE


def _service() -> DelegationService:
    return DelegationService(DescriptorRegistry([FAKE]), RutherfordConfig())


async def test_delegation_ok_with_files() -> None:
    request = DelegationRequest(
        target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), files=["a.py", "b.py"]
    )
    result = await _service().delegate(request)
    assert result.ok is True and "42" in result.text


async def test_delegation_unknown_agent() -> None:
    result = await _service().delegate(DelegationRequest(target=Target(cli="nope"), prompt="x"))
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN_TARGET


async def test_delegation_untrusted_write_is_refused() -> None:
    request = DelegationRequest(
        target=Target(cli="fake"), prompt="x", safety_mode=SafetyMode.WRITE, working_dir=str(REPO_ROOT)
    )
    result = await _service().delegate(request)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.WORKSPACE_NOT_TRUSTED


async def test_delegation_trusted_write_runs() -> None:
    request = DelegationRequest(
        target=Target(cli="fake"),
        prompt="what is 17 + 25?",
        safety_mode=SafetyMode.WRITE,
        working_dir=str(REPO_ROOT),
        trust_workspace=True,
    )
    result = await _service().delegate(request)
    assert result.ok is True


def test_post_prompt_safety_classification() -> None:
    side = EventJournal()
    side.append(JournalEvent(kind="fs_write", detail="x"))
    assert _post_prompt_safety(side) is ReexecutionSafety.SIDE_EFFECTED
    tool = EventJournal()
    tool.append(JournalEvent(kind="tool_call", tool_call_id="t"))
    assert _post_prompt_safety(tool) is ReexecutionSafety.AMBIGUOUS
    assert _post_prompt_safety(EventJournal()) is ReexecutionSafety.DUPLICATE_COST


async def test_run_turn_records_requested_model() -> None:
    result = await run_acp_turn(
        FAKE, "what is 17 + 25?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="fake-model"
    )
    assert result.ok is True and result.target.model == "fake-model"


async def test_run_turn_handshake_failure() -> None:
    dead = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
    result = await run_acp_turn(dead, "hi", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_HANDSHAKE_FAILED
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE


async def test_acp_session_reuses_one_live_session_across_turns() -> None:
    async with ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT)) as session:
        session_id = session.session_id
        assert session_id is not None and session.target.cli == "fake"
        first = await session.prompt("EMPTY please", timeout_s=60.0)
        assert first.ok is False  # the first turn produced no answer
        second = await session.prompt("what is 17 + 25?", timeout_s=60.0)
        assert second.ok is True and "42" in second.text  # second turn's journal is clean of the first
        assert session.session_id == session_id  # the same live session, not a re-spawn


async def test_acp_session_open_raises_on_bad_agent() -> None:
    bad = AgentDescriptor("bad", "Bad", ("this-binary-does-not-exist-xyz123",))
    session = ACPSession(bad, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    with pytest.raises(ACPHandshakeError) as exc:
        await session.open()
    assert exc.value.code is ErrorCode.ACP_SPAWN_FAILED
    assert exc.value.safety is ReexecutionSafety.SAFE
