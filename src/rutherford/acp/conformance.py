# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""ACP conformance probing: does an agent actually drive over ACP on this machine?

The operational backbone for a heterogeneous roster. Each descriptor is probed with a trivial read-only
turn and classified -- working, installed-but-broken (handshake failed), or not installed (spawn failed) --
so the roster is a set of verified agents, not a list of optimistic launch commands. This is ``doctor``
over ACP: it is the only trustworthy signal, since there is no cheap non-interactive auth check.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..domain.enums import SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.models import DelegationResult
from .descriptors import AgentDescriptor
from .permission import PermissionPolicy
from .session import run_acp_turn

#: A trivial, side-effect-free prompt that any working agent should answer.
_PROBE_PROMPT = "Respond with exactly the word: OK"


class ConformanceReport(BaseModel):
    """The outcome of probing one agent's ACP server with a real round trip."""

    agent_id: str
    #: ``ok`` (handshake + answered) | ``no_answer`` (answered empty / refused) | ``handshake_failed``
    #: (installed, but initialize/new_session failed) | ``not_installed`` (the launch command was not found)
    #: | ``error`` (some other failure).
    status: str
    installed: bool
    answered: bool
    detail: str
    duration_s: float


def classify(agent_id: str, result: DelegationResult) -> ConformanceReport:
    """Map a probe turn's :class:`DelegationResult` to a :class:`ConformanceReport`."""
    if result.ok:
        return ConformanceReport(
            agent_id=agent_id,
            status="ok",
            installed=True,
            answered=True,
            detail="handshake + prompt round trip succeeded",
            duration_s=result.duration_s,
        )
    code = result.error.code if result.error is not None else None
    message = result.error.message if result.error is not None else "unknown failure"
    if code is ErrorCode.ACP_SPAWN_FAILED:
        return ConformanceReport(
            agent_id=agent_id,
            status="not_installed",
            installed=False,
            answered=False,
            detail=message,
            duration_s=result.duration_s,
        )
    if code is ErrorCode.ACP_HANDSHAKE_FAILED:
        return ConformanceReport(
            agent_id=agent_id,
            status="handshake_failed",
            installed=True,
            answered=False,
            detail=message,
            duration_s=result.duration_s,
        )
    if code in (ErrorCode.ACP_EMPTY_ANSWER, ErrorCode.ACP_REFUSED):
        return ConformanceReport(
            agent_id=agent_id,
            status="no_answer",
            installed=True,
            answered=False,
            detail=message,
            duration_s=result.duration_s,
        )
    return ConformanceReport(
        agent_id=agent_id, status="error", installed=True, answered=False, detail=message, duration_s=result.duration_s
    )


async def probe_agent(descriptor: AgentDescriptor, *, cwd: str, timeout_s: float = 60.0) -> ConformanceReport:
    """Drive ``descriptor``'s agent with a trivial read-only turn and classify the outcome."""
    result = await run_acp_turn(
        descriptor, _PROBE_PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=cwd, timeout_s=timeout_s
    )
    return classify(descriptor.id, result)
