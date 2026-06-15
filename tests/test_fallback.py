# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for ReexecutionSafety-gated fallback (F7) and cooldown wiring into delegation + consensus.

The fallback decision is exercised with a controllable stub for ``run_acp_turn`` so each target's
``(ok, error.code, reexecution_safety)`` is dictated exactly -- the gating (SAFE only, non-mutating only,
benched skipped) and the recorded chain (``fallback_chain`` / ``fallback_from`` / ``delegation_call_count``)
are pure logic over those results, not a real subprocess. One test drives the REAL fake agent through a
genuine spawn-fail SAFE failure into a working fallback, to prove the wiring end to end.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rutherford.acp.cooldown import CooldownTracker
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import ReexecutionSafety, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import (
    DelegationRequest,
    DelegationResult,
    ErrorInfo,
    Provenance,
    Target,
)
from rutherford.services.consensus import ConsensusService
from rutherford.services.delegation import DelegationService

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))
BAD = AgentDescriptor("bad", "Bad", ("this-binary-does-not-exist-xyz123",))


# --- A controllable run_acp_turn stub ----------------------------------------

#: A per-target outcome the stub returns, keyed by the agent id. Each entry dictates one turn's result.
TurnPlan = dict[str, Callable[[str, str | None], DelegationResult]]


def _ok(cli: str, model: str | None) -> DelegationResult:
    return DelegationResult(
        target=Target(cli=cli, model=model),
        ok=True,
        text="42",
        provenance=Provenance(provider="fake", model=model, confirmed=False),
        safety_mode=SafetyMode.READ_ONLY,
    )


def _fail(code: ErrorCode, safety: ReexecutionSafety) -> Callable[[str, str | None], DelegationResult]:
    def build(cli: str, model: str | None) -> DelegationResult:
        return DelegationResult(
            target=Target(cli=cli, model=model),
            ok=False,
            error=ErrorInfo(code=code, message=f"{cli} failed: {code}", reexecution_safety=safety),
            safety_mode=SafetyMode.READ_ONLY,
        )

    return build


def _install_stub(monkeypatch: pytest.MonkeyPatch, plan: TurnPlan) -> list[tuple[str, str | None]]:
    """Patch ``run_acp_turn`` to return the planned result per agent id; return the attempt log (cli, model)."""
    attempts: list[tuple[str, str | None]] = []

    async def fake_run_acp_turn(descriptor: AgentDescriptor, prompt: str, **kwargs: Any) -> DelegationResult:
        model = kwargs.get("model")
        attempts.append((descriptor.id, model))
        builder = plan[descriptor.id]
        return builder(descriptor.id, model)

    monkeypatch.setattr("rutherford.services.delegation.run_acp_turn", fake_run_acp_turn)
    return attempts


def _registry(*descriptors: AgentDescriptor) -> DescriptorRegistry:
    return DescriptorRegistry(descriptors or (FAKE,))


def _service(
    config: RutherfordConfig | None = None,
    *,
    cooldown: CooldownTracker | None = None,
    descriptors: DescriptorRegistry | None = None,
) -> DelegationService:
    return DelegationService(descriptors or _registry(), config or RutherfordConfig(), cooldown=cooldown)


# --- ReexecutionSafety gating ------------------------------------------------


async def test_fallback_fires_on_a_safe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {
        "primary": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE),
        "backup": _ok,
    }
    attempts = _install_stub(monkeypatch, plan)
    service = _service(
        descriptors=_registry(AgentDescriptor("primary", "P", ("x",)), AgentDescriptor("backup", "B", ("x",)))
    )
    result = await service.delegate(
        DelegationRequest(target=Target(cli="primary"), prompt="hi", fallback=[Target(cli="backup")])
    )
    assert result.ok is True and result.text == "42"
    assert result.target.cli == "backup"  # the target is whoever finally answered
    assert result.fallback_chain == ["primary"]  # the failed primary leads the recorded chain
    assert result.delegation_call_count == 2  # primary attempt + the backup
    assert attempts == [("primary", None), ("backup", None)]


@pytest.mark.parametrize(
    "safety",
    [ReexecutionSafety.DUPLICATE_COST, ReexecutionSafety.AMBIGUOUS, ReexecutionSafety.SIDE_EFFECTED],
)
async def test_fallback_does_not_fire_on_an_unsafe_failure(
    monkeypatch: pytest.MonkeyPatch, safety: ReexecutionSafety
) -> None:
    plan: TurnPlan = {
        "primary": _fail(ErrorCode.ACP_REFUSED, safety),
        "backup": _ok,
    }
    attempts = _install_stub(monkeypatch, plan)
    service = _service(
        descriptors=_registry(AgentDescriptor("primary", "P", ("x",)), AgentDescriptor("backup", "B", ("x",)))
    )
    result = await service.delegate(
        DelegationRequest(target=Target(cli="primary"), prompt="hi", fallback=[Target(cli="backup")])
    )
    assert result.ok is False  # an unsafe failure is kept; the backup is never tried
    assert result.target.cli == "primary"
    assert result.fallback_chain is None
    assert result.delegation_call_count == 1
    assert attempts == [("primary", None)]  # the backup was not attempted


async def test_chain_tries_each_alternate_until_one_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {
        "primary": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE),
        "alt1": _fail(ErrorCode.ACP_HANDSHAKE_FAILED, ReexecutionSafety.SAFE),
        "alt2": _ok,
    }
    attempts = _install_stub(monkeypatch, plan)
    descriptors = _registry(
        AgentDescriptor("primary", "P", ("x",)),
        AgentDescriptor("alt1", "A1", ("x",)),
        AgentDescriptor("alt2", "A2", ("x",)),
    )
    result = await _service(descriptors=descriptors).delegate(
        DelegationRequest(target=Target(cli="primary"), prompt="hi", fallback=[Target(cli="alt1"), Target(cli="alt2")])
    )
    assert result.ok is True and result.target.cli == "alt2"
    assert result.fallback_chain == ["primary", "alt1"]  # both failures recorded, in order
    assert result.delegation_call_count == 3  # primary + alt1 + alt2
    assert [cli for cli, _ in attempts] == ["primary", "alt1", "alt2"]


async def test_exhausted_chain_keeps_the_primary_failure_and_counts_all(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {
        "primary": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE),
        "alt1": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE),
    }
    _install_stub(monkeypatch, plan)
    descriptors = _registry(AgentDescriptor("primary", "P", ("x",)), AgentDescriptor("alt1", "A1", ("x",)))
    result = await _service(descriptors=descriptors).delegate(
        DelegationRequest(target=Target(cli="primary"), prompt="hi", fallback=[Target(cli="alt1")])
    )
    assert result.ok is False and result.target.cli == "primary"  # the primary's refined failure is kept
    assert result.error is not None and result.error.code is ErrorCode.ACP_SPAWN_FAILED
    assert result.fallback_chain is None  # nothing recovered, so no chain is stamped on the kept primary
    assert result.delegation_call_count == 2  # both attempts still counted into realized fan-out


# --- Write-mode never falls back ---------------------------------------------


async def test_write_mode_delegation_never_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan: TurnPlan = {
        "primary": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE),
        "backup": _ok,
    }
    attempts = _install_stub(monkeypatch, plan)
    config = RutherfordConfig(trusted_workspaces=[str(tmp_path)])
    descriptors = _registry(AgentDescriptor("primary", "P", ("x",)), AgentDescriptor("backup", "B", ("x",)))
    result = await _service(config, descriptors=descriptors).delegate(
        DelegationRequest(
            target=Target(cli="primary"),
            prompt="hi",
            fallback=[Target(cli="backup")],
            safety_mode=SafetyMode.WRITE,
            working_dir=str(tmp_path),
            trust_workspace=True,
        )
    )
    assert result.ok is False and result.target.cli == "primary"  # a write that may have mutated is not re-run
    assert result.fallback_chain is None
    assert attempts == [("primary", None)]  # the backup was never tried


# --- Model fallback (same agent) ---------------------------------------------


async def test_model_fallback_retries_same_agent_on_its_fallback_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_then_ok(cli: str, model: str | None) -> DelegationResult:
        # The first model is unavailable (SAFE); the fallback model answers.
        if model == "good":
            return _ok(cli, model)
        return DelegationResult(
            target=Target(cli=cli, model=model),
            ok=False,
            error=ErrorInfo(
                code=ErrorCode.ACP_SPAWN_FAILED,
                message="unknown model: bad",
                reexecution_safety=ReexecutionSafety.SAFE,
            ),
            safety_mode=SafetyMode.READ_ONLY,
        )

    attempts: list[tuple[str, str | None]] = []

    async def fake_run(descriptor: AgentDescriptor, prompt: str, **kwargs: Any) -> DelegationResult:
        model = kwargs.get("model")
        attempts.append((descriptor.id, model))
        return fail_then_ok(descriptor.id, model)

    monkeypatch.setattr("rutherford.services.delegation.run_acp_turn", fake_run)
    descriptors = _registry(AgentDescriptor("agent", "A", ("x",), fallback_model="good"))
    result = await _service(descriptors=descriptors).delegate(
        DelegationRequest(target=Target(cli="agent", model="bad"), prompt="hi")
    )
    assert result.ok is True and result.target.model == "good"
    assert result.fallback_from == "bad"  # the originally requested model is recorded
    assert result.delegation_call_count == 2
    assert attempts == [("agent", "bad"), ("agent", "good")]


async def test_model_fallback_is_a_noop_without_a_configured_fallback_model(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {"agent": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE)}
    # The message looks model-unavailable, but the agent declares no fallback_model -> clean no-op.
    plan["agent"] = lambda cli, model: DelegationResult(
        target=Target(cli=cli, model=model),
        ok=False,
        error=ErrorInfo(
            code=ErrorCode.ACP_SPAWN_FAILED, message="unknown model", reexecution_safety=ReexecutionSafety.SAFE
        ),
        safety_mode=SafetyMode.READ_ONLY,
    )
    attempts = _install_stub(monkeypatch, plan)
    descriptors = _registry(AgentDescriptor("agent", "A", ("x",)))  # no fallback_model
    result = await _service(descriptors=descriptors).delegate(
        DelegationRequest(target=Target(cli="agent", model="bad"), prompt="hi")
    )
    assert result.ok is False
    assert result.fallback_from is None
    assert result.delegation_call_count == 1
    assert attempts == [("agent", "bad")]  # only the one attempt; no model fallback


# --- Cooldown wiring ----------------------------------------------------------


async def test_unhealthy_failures_bench_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {"agent": _fail(ErrorCode.ACP_TURN_TIMEOUT, ReexecutionSafety.AMBIGUOUS)}
    _install_stub(monkeypatch, plan)
    cooldown = CooldownTracker(threshold=2, window_s=120.0, duration_s=60.0)
    service = _service(descriptors=_registry(AgentDescriptor("agent", "A", ("x",))), cooldown=cooldown)
    req = DelegationRequest(target=Target(cli="agent"), prompt="hi")
    await service.delegate(req)
    assert cooldown.is_benched("agent") is False  # one unhealthy failure, below the threshold
    await service.delegate(req)
    assert cooldown.is_benched("agent") is True  # the second trips the bench


async def test_clean_failure_does_not_bench(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {"agent": _fail(ErrorCode.ACP_REFUSED, ReexecutionSafety.DUPLICATE_COST)}
    _install_stub(monkeypatch, plan)
    cooldown = CooldownTracker(threshold=2, window_s=120.0, duration_s=60.0)
    service = _service(descriptors=_registry(AgentDescriptor("agent", "A", ("x",))), cooldown=cooldown)
    req = DelegationRequest(target=Target(cli="agent"), prompt="hi")
    for _ in range(5):
        await service.delegate(req)
    assert cooldown.is_benched("agent") is False  # a refusal is the request's fault, never benches


async def test_success_resets_the_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"fail": True}

    async def fake_run(descriptor: AgentDescriptor, prompt: str, **kwargs: Any) -> DelegationResult:
        if state["fail"]:
            return _fail(ErrorCode.ACP_TURN_TIMEOUT, ReexecutionSafety.AMBIGUOUS)(descriptor.id, kwargs.get("model"))
        return _ok(descriptor.id, kwargs.get("model"))

    monkeypatch.setattr("rutherford.services.delegation.run_acp_turn", fake_run)
    cooldown = CooldownTracker(threshold=3, window_s=120.0, duration_s=60.0)
    service = _service(descriptors=_registry(AgentDescriptor("agent", "A", ("x",))), cooldown=cooldown)
    req = DelegationRequest(target=Target(cli="agent"), prompt="hi")
    await service.delegate(req)
    await service.delegate(req)
    state["fail"] = False
    await service.delegate(req)  # a success clears the streak
    state["fail"] = True
    await service.delegate(req)
    await service.delegate(req)
    assert cooldown.is_benched("agent") is False  # only two failures since the reset


async def test_explicit_delegate_to_a_benched_agent_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {"agent": _ok}
    _install_stub(monkeypatch, plan)
    cooldown = CooldownTracker(threshold=1, window_s=120.0, duration_s=60.0)
    cooldown.record_failure("agent")  # bench it up front
    assert cooldown.is_benched("agent") is True
    service = _service(descriptors=_registry(AgentDescriptor("agent", "A", ("x",))), cooldown=cooldown)
    result = await service.delegate(DelegationRequest(target=Target(cli="agent"), prompt="hi"))
    assert result.ok is True  # an explicit, caller-chosen delegation runs regardless of the bench


async def test_benched_alternate_is_skipped_in_the_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    plan: TurnPlan = {
        "primary": _fail(ErrorCode.ACP_SPAWN_FAILED, ReexecutionSafety.SAFE),
        "benched": _ok,  # would answer if it ran -- but it is benched, so it must be skipped
        "healthy": _ok,
    }
    attempts = _install_stub(monkeypatch, plan)
    cooldown = CooldownTracker(threshold=1, window_s=120.0, duration_s=60.0)
    cooldown.record_failure("benched")
    descriptors = _registry(
        AgentDescriptor("primary", "P", ("x",)),
        AgentDescriptor("benched", "B", ("x",)),
        AgentDescriptor("healthy", "H", ("x",)),
    )
    service = _service(descriptors=descriptors, cooldown=cooldown)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="primary"), prompt="hi", fallback=[Target(cli="benched"), Target(cli="healthy")]
        )
    )
    assert result.ok is True and result.target.cli == "healthy"  # skipped the benched alternate, took the healthy one
    assert result.fallback_chain == ["primary", "benched (benched)"]  # the bench skip is visible in the chain
    assert [cli for cli, _ in attempts] == ["primary", "healthy"]  # benched was never attempted


# --- Cooldown wiring into the consensus auto-panel ---------------------------


async def test_benched_agent_is_skipped_in_expand_all() -> None:
    config = RutherfordConfig()
    descriptors = _registry(
        AgentDescriptor("alpha", "Alpha", ("x",)),
        AgentDescriptor("beta", "Beta", ("x",)),
    )
    cooldown = CooldownTracker(threshold=1, window_s=120.0, duration_s=60.0)
    cooldown.record_failure("beta")  # bench beta
    delegation = DelegationService(descriptors, config, cooldown=cooldown)
    consensus = ConsensusService(delegation, descriptors, config, cooldown=cooldown)
    included, skipped = consensus._expand_all()
    assert [t.cli for t in included] == ["alpha"]  # the benched agent is left out
    assert any(s.cli == "beta" and "benched" in s.reason for s in skipped)


# --- Live-fake end-to-end: a real spawn-fail SAFE failure falls back ----------


async def test_real_spawn_failure_falls_back_to_the_working_fake() -> None:
    """A genuine spawn-fail SAFE failure on a bad command falls back to the real fake agent (no stub)."""
    descriptors = DescriptorRegistry([BAD, FAKE])
    service = DelegationService(descriptors, RutherfordConfig())
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="bad"),
            prompt="what is 17 + 25?",
            working_dir=str(REPO_ROOT),
            fallback=[Target(cli="fake")],
        )
    )
    assert result.ok is True and "42" in result.text
    assert result.target.cli == "fake"
    assert result.fallback_chain == ["bad"]
    assert result.delegation_call_count == 2


async def test_consensus_takes_cooldown_default_when_not_injected() -> None:
    """A ConsensusService built without an explicit tracker benches nobody (disabled), so expand_all is full."""
    config = RutherfordConfig()
    descriptors = _registry(AgentDescriptor("alpha", "Alpha", ("x",)), AgentDescriptor("beta", "Beta", ("x",)))
    delegation = DelegationService(descriptors, config)
    consensus = ConsensusService(delegation, descriptors, config)  # no cooldown injected
    included, skipped = consensus._expand_all()
    assert {t.cli for t in included} == {"alpha", "beta"}
    assert skipped == []
