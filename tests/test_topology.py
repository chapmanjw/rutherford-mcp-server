# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for N1 topology + live transparency + the cross-cutting limits (item 3 / reliability).

Covers, against the fake ACP agent (no real CLI): the :class:`Topology` populated on a panel + a delegate
result; the per-voice activity snapshot the ``activity`` tool projects; the progress callback firing with an
increasing fraction; the ``max_concurrency`` semaphore actually serializing a wide panel; the agent cap
(``over_cap`` flag and the ``enforce_agent_cap`` -> ``AGENT_CAP_EXCEEDED`` refusal); the ``max_depth`` guard
(``RUTHERFORD_DEPTH`` -> ``MAX_DEPTH_EXCEEDED``); and the lineage/depth env propagated into the spawned agent.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import ActivityEventKind
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import (
    ActivityEvent,
    ConsensusRequest,
    ConsensusResult,
    DebateRequest,
    DelegationRequest,
    Target,
)
from rutherford.runtime.depth import ENV_DEPTH
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
FAKE_A = AgentDescriptor("fake_a", "Fake A", _FAKE_CMD, provider="alpha", default_model="model-a")
FAKE_B = AgentDescriptor("fake_b", "Fake B", _FAKE_CMD, provider="beta", default_model="model-b")
# A slow agent: streams a partial then sleeps, so a panel can mix a fast and a slow voice, or a serialized
# pair takes measurably longer than a parallel one. The 0.4s sleep is sub-second on purpose: the semaphore
# bounds concurrency at asyncio resolution, so a serialized pair (two 0.4s sleeps back-to-back, ~0.8s) only
# needs a measurable gap over the parallel control (~0.4s) -- whole seconds add nothing the gap doesn't.
SLOW = AgentDescriptor("slow", "Slow", _FAKE_CMD, env_overrides=(("RUTHERFORD_FAKE_SLEEP", "0.4"),))


def _registry(extra: list[AgentDescriptor] | None = None) -> DescriptorRegistry:
    return DescriptorRegistry([FAKE, FAKE_A, FAKE_B, *(extra or [])])


def _consensus(config: RutherfordConfig | None = None, extra: list[AgentDescriptor] | None = None) -> ConsensusService:
    resolved = config or RutherfordConfig()
    registry = _registry(extra)
    return ConsensusService(DelegationService(registry, resolved), registry, resolved)


def _debate(config: RutherfordConfig | None = None, extra: list[AgentDescriptor] | None = None) -> DebateService:
    resolved = config or RutherfordConfig()
    registry = _registry(extra)
    return DebateService(registry, resolved, DelegationService(registry, resolved))


# --- A. Topology -------------------------------------------------------------


async def test_consensus_topology_declared_and_realized() -> None:
    """A two-voice consensus reports declared=2, realized=2 (one delegation per voice), observed >= 0."""
    request = ConsensusRequest(
        targets=[Target(cli="fake_a"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    result = await _consensus().consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.topology is not None
    topology = result.topology
    assert topology.declared == 2
    assert topology.realized_delegations == 2  # one subprocess delegation per voice, no fallback
    # observed is a floor sampled from the live process tree; a fast fake may finish before the first sample,
    # so it is either None or a non-negative count -- never wrong-signed.
    assert topology.observed_peak_agents is None or topology.observed_peak_agents >= 0
    assert topology.over_cap is False


async def test_delegate_topology_single() -> None:
    """A single delegation reports realized=1 (the declared slot is the consensus concept, not delegate's)."""
    registry = _registry()
    service = DelegationService(registry, RutherfordConfig())
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    )
    assert result.ok is True
    assert result.delegation_call_count == 1
    assert result.observed_peak_agents is None or result.observed_peak_agents >= 0


async def test_debate_topology_sums_across_turns() -> None:
    """A two-voice, two-round debate sums realized across every turn (2 voices x 2 rounds = 4)."""
    request = DebateRequest(
        targets=[Target(cli="fake"), Target(cli="fake")],
        prompt="what is 17 + 25?",
        rounds=2,
        working_dir=str(REPO_ROOT),
    )
    result = await _debate().debate(request)
    assert result.topology is not None
    assert result.topology.declared == 2
    assert result.topology.realized_delegations == 4  # one delegation per turn, two rounds of two voices


# --- D2. Agent cap -----------------------------------------------------------


async def test_over_cap_flags_topology_but_does_not_refuse() -> None:
    """A panel wider than the advisory cap is flagged over_cap (informational), not refused, by default."""
    config = RutherfordConfig(max_agents_advisory=2)  # cap below the 3-voice width below
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake_a"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    result = await _consensus(config).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert len(result.voices) == 3  # the panel still ran -- advisory, not blocking
    assert result.topology is not None and result.topology.over_cap is True


async def test_enforce_agent_cap_refuses_up_front() -> None:
    """With enforce_agent_cap, an over-cap declared width is refused with AGENT_CAP_EXCEEDED before any voice."""
    config = RutherfordConfig(max_agents_advisory=2, enforce_agent_cap=True)
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake_a"), Target(cli="fake_b")],
        prompt="x",
        working_dir=str(REPO_ROOT),
    )
    with pytest.raises(RutherfordError) as exc:
        await _consensus(config).consensus(request)
    assert exc.value.code is ErrorCode.AGENT_CAP_EXCEEDED


async def test_within_cap_is_not_over_cap() -> None:
    """A panel within the advisory cap is never flagged over_cap."""
    config = RutherfordConfig(max_agents_advisory=4)
    request = ConsensusRequest(
        targets=[Target(cli="fake"), Target(cli="fake_a")], prompt="what is 17 + 25?", working_dir=str(REPO_ROOT)
    )
    result = await _consensus(config).consensus(request)
    assert isinstance(result, ConsensusResult)
    assert result.topology is not None and result.topology.over_cap is False


# --- D1. max_concurrency semaphore -------------------------------------------


async def _slow_panel_elapsed(max_concurrency: int) -> tuple[float, ConsensusResult]:
    """Run a two-slow-voice panel at ``max_concurrency`` and return the wall-clock and result."""
    config = RutherfordConfig(max_concurrency=max_concurrency)
    service = _consensus(config, extra=[SLOW])
    request = ConsensusRequest(
        targets=[Target(cli="slow"), Target(cli="slow")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    start = time.monotonic()
    result = await service.consensus(request)
    assert isinstance(result, ConsensusResult)
    return time.monotonic() - start, result


async def test_semaphore_serializes_a_wide_panel() -> None:
    """With max_concurrency=1, two slow voices run SERIALLY, taking measurably longer than running in parallel.

    The fake sleeps 0.4s per voice. Serialized that is ~0.8s of sleep; in parallel the sleeps overlap (~0.4s).
    Asserting the serial run is measurably SLOWER than the parallel control proves the semaphore bounded
    concurrency to one -- a relative comparison, robust to per-machine spawn overhead that a fixed floor is not.
    The extra sleep the serialization forces (~0.4s) is the invariant; we require a margin well above timing
    jitter (>0.2s) rather than tight wall-clock equality, so a loaded machine does not misorder the two runs.
    """
    serial_elapsed, serial = await _slow_panel_elapsed(max_concurrency=1)
    parallel_elapsed, _ = await _slow_panel_elapsed(max_concurrency=2)
    assert len(serial.voices) == 2 and all(v.ok for v in serial.voices)
    # Serialized adds ~one extra 0.4s sleep over the parallel run; require at least 0.2s of separation (half the
    # nominal gap) so jitter cannot flip the order, and check the serial run is at least ~1.5x the parallel run.
    assert serial_elapsed - parallel_elapsed > 0.2, (
        f"serial={serial_elapsed:.2f}s parallel={parallel_elapsed:.2f}s -- the semaphore did not serialize"
    )
    assert serial_elapsed > 1.5 * parallel_elapsed, (
        f"serial={serial_elapsed:.2f}s parallel={parallel_elapsed:.2f}s -- serial was not ~1.5x the parallel run"
    )


# --- B. Per-voice activity stream + snapshot ---------------------------------


async def test_consensus_emits_per_voice_activity() -> None:
    """A consensus emits panel_started, a started+finished per voice, and panel_finished -- the live stream."""
    events: list[ActivityEvent] = []
    request = ConsensusRequest(
        targets=[Target(cli="fake_a"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    await _consensus().consensus(request, on_activity=events.append)
    kinds = [event.kind for event in events]
    assert kinds[0] is ActivityEventKind.PANEL_STARTED
    assert kinds[-1] is ActivityEventKind.PANEL_FINISHED
    started = [e for e in events if e.kind is ActivityEventKind.VOICE_STARTED]
    finished = [e for e in events if e.kind is ActivityEventKind.VOICE_FINISHED]
    assert len(started) == 2 and len(finished) == 2
    # Each finished voice carries the per-voice columns the activity table needs.
    for event in finished:
        assert event.cli in {"fake_a", "fake_b"}
        assert event.status == "ok"
        assert event.correlation_id is not None


def test_activity_snapshot_in_flight_columns() -> None:
    """The per-voice activity projection collapses a voice's events to its current row with the 3-H columns."""
    from rutherford.tools.jobs import _latest_voice_states

    events = [
        ActivityEvent(kind=ActivityEventKind.PANEL_STARTED, tool="consensus", declared=2),
        ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, correlation_id="voice:0", cli="fake_a", status="started"),
        ActivityEvent(kind=ActivityEventKind.VOICE_STARTED, correlation_id="voice:1", cli="fake_b", status="started"),
        ActivityEvent(
            kind=ActivityEventKind.VOICE_FINISHED,
            correlation_id="voice:0",
            cli="fake_a",
            model="model-a",
            status="ok",
            elapsed_s=0.4,
            observed_agents=1,
        ),
    ]
    rows = _latest_voice_states(events)
    by_cid = {event.correlation_id: event for event in rows}
    # voice:0 collapsed to its terminal (ok) row; voice:1 still shows its in-flight (started) row.
    assert by_cid["voice:0"].status == "ok" and by_cid["voice:0"].observed_agents == 1
    assert by_cid["voice:1"].status == "started"
    # the panel-level event (no cli) is not a voice row.
    assert all(event.cli is not None for event in rows)


# --- C. Progress callback (consensus fraction) -------------------------------


async def test_progress_callback_fires_with_increasing_fraction() -> None:
    """The progress pusher maps the activity stream to (done, total) pairs that climb to the declared total."""
    from rutherford.server import make_progress_pusher

    fractions: list[tuple[float, float | None]] = []

    class _Ctx:
        async def report_progress(self, progress: float, total: float | None, message: str | None) -> None:
            fractions.append((progress, total))

    push = make_progress_pusher(_Ctx())  # type: ignore[arg-type]  # a stub Context with just report_progress
    request = ConsensusRequest(
        targets=[Target(cli="fake_a"), Target(cli="fake_b")],
        prompt="what is 17 + 25?",
        working_dir=str(REPO_ROOT),
    )
    await _consensus().consensus(request, on_activity=push)
    await asyncio.sleep(0.05)  # the pushes are fire-and-forget tasks; let them drain
    assert fractions, "the progress callback never fired"
    # The total is the declared width (2) once the panel started; the done count climbs to it monotonically.
    totals = {total for _done, total in fractions if total is not None}
    assert totals == {2.0}
    done_values = [done for done, total in fractions if total is not None]
    assert done_values == sorted(done_values)  # non-decreasing
    assert max(done_values) == 2.0  # reaches 100% at panel_finished


# --- D3. Lineage / depth -----------------------------------------------------


async def test_max_depth_refuses_a_too_deep_call() -> None:
    """A delegation at base_depth == max_depth is refused with MAX_DEPTH_EXCEEDED before spawning."""
    config = RutherfordConfig(max_depth=2)
    service = DelegationService(_registry(), config)
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="x", working_dir=str(REPO_ROOT)),
        base_depth=2,  # at the ceiling
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.MAX_DEPTH_EXCEEDED


async def test_within_depth_runs() -> None:
    """A delegation below the ceiling runs normally."""
    config = RutherfordConfig(max_depth=3)
    service = DelegationService(_registry(), config)
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT)),
        base_depth=2,
    )
    assert result.ok is True


async def test_lineage_env_propagated_to_the_spawned_agent() -> None:
    """The spawned agent sees RUTHERFORD_DEPTH set to base_depth + 1 in its own environment.

    The fake echoes the value of the env var an ``ENV=<name>`` token names, so this asserts Rutherford
    actually layered the lineage/depth signal onto the child process environment.
    """
    service = DelegationService(_registry(), RutherfordConfig())
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt=f"ENV={ENV_DEPTH}", working_dir=str(REPO_ROOT)),
        base_depth=1,
    )
    assert result.ok is True
    assert result.text.strip() == f"{ENV_DEPTH}=2"  # base_depth 1 -> child depth 2


async def test_lineage_count_propagated_to_the_spawned_agent() -> None:
    """The spawned agent sees RUTHERFORD_LINEAGE set (count-first), so an aggregate cap can reason across layers."""
    service = DelegationService(_registry(), RutherfordConfig())
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="ENV=RUTHERFORD_LINEAGE", working_dir=str(REPO_ROOT)),
    )
    assert result.ok is True
    # at the top level the in-process lineage count is 0, so the child is 1.
    assert result.text.strip() == "RUTHERFORD_LINEAGE=1"
