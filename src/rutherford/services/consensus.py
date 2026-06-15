# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The consensus service: ask several ACP agents the same prompt in parallel and aggregate the voices.

Where the delegation service hands a prompt to one agent, consensus fans it out to N agents concurrently
(each its own ACP session) and reduces the result. With ``all-voices`` (the default) every voice is
returned unchanged, with an optional server-side synthesis and a diversity report. With a real strategy
(``unanimous`` / ``majority`` / ``plurality`` / ``weighted`` / ``parity-pair``) each voice is asked for a
verdict and the panel collapses to one :class:`StrategyResult` outcome. One failing voice is a failed
:class:`DelegationResult` (or :class:`VoiceVerdict`) in the result, never an aborted panel. ``expand_all``
builds the panel from every registered agent (capped at ``max_targets``), recording each exclusion's
reason in ``skipped``. The per-seat ``Target`` metadata -- role, weight, parity, stance -- steers each
voice and feeds the strategies.
"""

from __future__ import annotations

import asyncio
import logging
import random
import string
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..acp.cooldown import CooldownTracker
from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession, run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import EFFORT_ORDER, ActivityEventKind, SafetyMode, Stance, Strategy, runs_sandboxed
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    ActivityEvent,
    ConsensusRequest,
    ConsensusResult,
    Cost,
    DelegationRequest,
    DelegationResult,
    DiversityReport,
    ErrorInfo,
    PanelInputs,
    PanelTarget,
    Provenance,
    RankEntry,
    RankReport,
    RunRollup,
    SkippedTarget,
    StrategyResult,
    Target,
    Topology,
    VoiceVerdict,
)
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_aggregate_cap
from .delegation import ActivityCallback, DelegationService, PanelLifecycle, emit_activity
from .persistence import PanelVoice, render_panel_voice_files, write_panel_record
from .strategies import (
    aggregate,
    apply_stance,
    effective_diversity,
    extract_ranking,
    extract_verdict,
    rank_panel,
    ranking_instruction,
    verdict_instruction,
)

_log = logging.getLogger("rutherford.services.consensus")


@dataclass(frozen=True)
class _Candidate:
    """One answer that RANK is ranking: its position, the seat it came from, and its display identity."""

    pos: int
    target_index: int
    label: str
    cli: str
    model: str | None
    text: str
    provenance: Provenance | None


class ConsensusService:
    """Runs a consensus panel across ACP agents, with strategies, synthesis, and diversity scoring."""

    def __init__(
        self,
        delegation: DelegationService,
        descriptors: DescriptorRegistry,
        config: RutherfordConfig,
        *,
        cooldown: CooldownTracker | None = None,
        ledger: RunLedger | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._delegation = delegation
        self._descriptors = descriptors
        self._config = config
        #: The RNG that anonymizes + shuffles each RANK ballot (F4b, 7-D). Injectable so a test can pin the
        #: shuffle with a seeded ``random.Random``; defaults to an unseeded instance in production.
        self._rng = rng or random.Random()
        #: The durable run ledger (F2) for the panel's parent record; ``None`` disables persistence. The
        #: per-voice child records are written by the delegation service as each voice runs.
        self._ledger = ledger
        #: Wall-clock source for parent-record timestamps, injectable so persistence is testable.
        self._clock = clock
        #: The per-agent cooldown tracker (F7): an auto-expanded (``expand_all``) panel leaves a benched agent
        #: OUT (recorded in ``skipped`` with the time remaining), since auto-selection should not keep reaching
        #: for a flapping seat. ``cooldown`` is injected so it is the SAME tracker the delegation primitive
        #: records health into (the skip reflects the bench a delegation just set); ``None`` keeps cooldown out
        #: of the way for a directly-constructed service (a disabled tracker -- no agent is ever benched).
        self._cooldown = cooldown or CooldownTracker(threshold=0, window_s=1.0, duration_s=1.0)

    async def consensus(
        self,
        req: ConsensusRequest,
        *,
        base_depth: int = 0,
        on_activity: ActivityCallback | None = None,
    ) -> ConsensusResult | StrategyResult:
        """Fan ``req`` out across its targets and reduce the voices.

        Wraps the panel body in a :class:`PanelLifecycle` (N1, item 3, decision 3-K) so the activity stream
        always closes with exactly one terminal event -- ``panel_finished`` on a clean or budget-exhausted
        finish, ``job_cancelled`` if a cancel lands at any await. ``on_activity`` is the structured live
        stream (the sync push / the job poll buffer); ``base_depth`` is how deep the panel sits in a
        Rutherford-driving-Rutherford chain, propagated to every voice. See :meth:`_consensus_impl`.
        """
        lifecycle = PanelLifecycle("consensus", base_depth, on_activity)
        try:
            return await self._consensus_impl(req, lifecycle, base_depth=base_depth, on_activity=on_activity)
        except asyncio.CancelledError:
            lifecycle.on_cancel()
            raise

    async def _consensus_impl(
        self,
        req: ConsensusRequest,
        lifecycle: PanelLifecycle,
        *,
        base_depth: int = 0,
        on_activity: ActivityCallback | None = None,
    ) -> ConsensusResult | StrategyResult:
        """The consensus panel body; the public :meth:`consensus` wraps this with the lifecycle guard.

        With ``expand_all`` (or an empty/``"all"`` target list resolved upstream), the panel is every
        registered agent capped at ``max_targets`` (excluded agents recorded in ``skipped``); otherwise it
        is the explicit, cap-checked ``targets``. A failing target is its own structured voice, so one bad
        voice never aborts the panel. A ``time_budget_s`` caps the whole panel's wall-clock: at the deadline
        the answered voices are kept and the in-flight ones are cut (their partial harvested), then the panel
        aggregates over the harvest as long as ``min_quorum`` usable voices remain -- below that floor it is
        ``BUDGET_EXHAUSTED`` (F8a). With a ``strategy`` other than ``all-voices`` the voices are aggregated
        into a :class:`StrategyResult`; otherwise the :class:`ConsensusResult` (every voice, plus an optional
        synthesis and a diversity report) is returned. Either shape carries ``stop_reason`` + a ``rollup``
        when a budget governed the run, and a populated :class:`Topology`.
        """
        # A consensus panel is read-only deliberation: it fans ONE question out to MANY agents. A sandboxed
        # (propose / write / yolo) mode has no coherent panel semantics -- there is no defined merge of edits
        # from several agents to one working tree -- and the budgeted-harvest path drives sessions directly in
        # the real working_dir with no per-turn sandbox, so a mutating mode there would write straight into the
        # user's tree. So a mutating mode is refused here (the service is the security boundary, not the tool);
        # writes go through delegate, which isolates a single agent in a worktree and applies the diff back.
        if runs_sandboxed(req.safety_mode):
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"consensus runs read-only: it cannot run '{req.safety_mode.value}'. It asks many agents the "
                "same question; there is no coherent way to apply edits from several of them to one tree. Use "
                "delegate (a single sandboxed agent) for write / propose work.",
            )
        created_at = self._clock()
        mono_start = time.monotonic()  # for the RANK round-2 deadline (the time budget spans both rounds)
        persist = self._config.wants_persist(req.persist)
        # When persisting, mint the parent id up front so each voice can be written as its child (F2). When not
        # persisting (or no ledger), the voices never self-persist (no orphan per-voice records).
        parent_run_id = uuid.uuid4().hex if persist and self._ledger is not None else None
        targets, skipped = self._resolve_targets(req)

        # N1 (item 3): the declared fan-out width. Check it against the advisory aggregate-agent cap up front
        # (a no-op unless one is configured; refuses with AGENT_CAP_EXCEEDED only when ``enforce_agent_cap``
        # is also set), then announce the panel as started so a sync caller is pushed the total before voices
        # run. ``over_cap`` (the advisory case) rides the topology at the end.
        declared = len(targets)
        over_cap = ensure_within_aggregate_cap(
            declared, self._config.max_agents_advisory, enforce=self._config.enforce_agent_cap
        )
        if over_cap:
            _log.warning(
                "consensus declared width %d exceeds the aggregate-agent cap %d; watch the activity view",
                declared,
                self._config.max_agents_advisory,
            )
        lifecycle.mark_started(
            ActivityEvent(
                kind=ActivityEventKind.PANEL_STARTED,
                tool="consensus",
                depth=base_depth,
                declared=declared,
                message=f"consensus panel started: {declared} voice(s)",
            )
        )

        budget = req.time_budget_s if req.time_budget_s is not None else self._config.default_time_budget_s
        voices, cut, stop_reason, elapsed_s = await self._fan_out(
            req, targets, budget, base_depth, on_activity, parent_run_id
        )

        if stop_reason == "budget":
            usable = sum(1 for voice in voices if voice.ok and voice.text.strip())
            if usable < self._config.min_quorum:
                lifecycle.mark_closed(
                    ActivityEvent(
                        kind=ActivityEventKind.PANEL_FINISHED,
                        tool="consensus",
                        depth=base_depth,
                        declared=declared,
                        done=usable,
                        status="failed",
                        message=f"consensus budget exhausted: {usable} usable voice(s), below quorum",
                    )
                )
                raise RutherfordError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"time budget ({budget:.0f}s) reached with {usable} usable voice(s), below "
                    f"min_quorum ({self._config.min_quorum})",
                )

        rollup = self._rollup(req, voices, cut, budget, stop_reason, elapsed_s) if budget is not None else None
        topology = self._topology(declared, voices, over_cap)

        effective_synthesize = req.synthesize if req.synthesize is not None else self._config.synthesize_default
        if req.strategy is Strategy.RANK:
            # RANK is a two-round protocol (F4b): the answers are in; now run the anonymized ranking round.
            # ``on_budget="continue"`` makes the budget advisory (round 1 ran every voice to completion), so the
            # ranking round must NOT be budget-skipped either -- pass no budget so round 2 always runs too.
            on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
            rank_budget = None if on_budget == "continue" else budget
            strategy_result = await self._rank(
                req, targets, voices, skipped, base_depth, on_activity, rank_budget, mono_start
            )
            answer = strategy_result.decision or strategy_result.outcome
            result: ConsensusResult | StrategyResult = strategy_result
        elif req.strategy is not Strategy.ALL_VOICES:
            strategy_result = self._aggregate(req, targets, voices, skipped)
            # The parent record's headline answer for a strategy panel is the decision (or, absent one, the
            # outcome category) so a reader opening state.json sees the panel's verdict, not "(no synthesis)".
            answer = strategy_result.decision or strategy_result.outcome
            result = strategy_result
        else:
            synthesis, synthesis_by, self_authored = await self._maybe_synthesize(req, voices, base_depth)
            answer = synthesis or "(no synthesis -- see the linked voice records)"
            result = ConsensusResult(
                voices=voices,
                synthesis=synthesis,
                synthesis_by=synthesis_by,
                self_authored=self_authored,
                skipped=skipped,
                diversity=self._diversity(voices),
            )
        result.stop_reason = stop_reason
        result.rollup = rollup
        result.topology = topology

        if parent_run_id is not None and self._ledger is not None:
            result.run_dir = await asyncio.to_thread(
                self._write_parent,
                req,
                parent_run_id,
                voices,
                skipped,
                answer,
                created_at,
                effective_synthesize,
                stop_reason,
                rollup,
                topology,
            )

        ok_count = sum(1 for voice in voices if voice.ok)
        # Surface the F3 effective-lineages headline on the transparency stream (item 5, 5-C): a reader sees
        # "3/5 ok -- 2 effective lineages; LOW DIVERSITY" live, not just buried in the result's diversity block.
        finished_message = f"consensus panel finished: {ok_count}/{len(voices)} ok"
        if result.diversity is not None:
            finished_message += f" -- {result.diversity.headline}"
        lifecycle.mark_closed(
            ActivityEvent(
                kind=ActivityEventKind.PANEL_FINISHED,
                tool="consensus",
                depth=base_depth,
                declared=declared,
                done=ok_count,
                observed_agents=topology.observed_peak_agents,
                message=finished_message,
            )
        )
        return result

    def _write_parent(
        self,
        req: ConsensusRequest,
        parent_run_id: str,
        voices: list[DelegationResult],
        skipped: list[SkippedTarget],
        answer: str,
        created_at: float,
        synthesize: bool,
        stop_reason: str | None,
        rollup: RunRollup | None,
        topology: Topology,
    ) -> str | None:
        """Write the panel's parent record linking each voice's child record, plus the voice artifacts (F2).

        The parent's status derives from the voices, and one ``voices/voice-N.md`` per voice (plus
        ``skipped.md`` for an auto-panel's left-out agents) makes the parent auditable without every child
        record still on disk. :class:`PanelInputs` captures the resolved orchestration config (roster +
        per-seat stance, the aggregation strategy, whether a synthesis ran, any judge) so the panel replays
        from here. Best-effort: a write failure returns ``None`` and the panel keeps its answer. Runs
        off-thread (file I/O) via :meth:`_consensus_impl`.
        """
        assert self._ledger is not None  # guarded by the caller (parent_run_id set only when ledger present)
        panel_voices = [_panel_voice(voice) for voice in voices]
        skipped_pairs = [(entry.cli, entry.reason) for entry in skipped]
        panel_inputs = PanelInputs(
            targets=[
                PanelTarget(
                    cli=voice.target.cli,
                    model=voice.target.model,
                    stance=_stance_for(voice.target, req.stances, index),
                    session_id=voice.session_id,
                )
                for index, voice in enumerate(voices)
            ],
            strategy=req.strategy.value,
            synthesize=synthesize,
            judge=req.judge.display_label if req.judge else None,
        )
        return write_panel_record(
            self._ledger,
            run_id=parent_run_id,
            kind="consensus",
            prompt=req.prompt,
            clis=sorted({voice.target.cli for voice in voices}),
            voices=panel_voices,
            answer=answer,
            created_at=created_at,
            finished_at=self._clock(),
            safety_mode=req.safety_mode,
            cwd=req.working_dir,
            files=req.files,
            role=req.role,
            panel=panel_inputs,
            stop_reason=stop_reason,
            rollup=rollup,
            topology=topology,
            extra_artifacts=render_panel_voice_files(panel_voices, skipped_pairs),
        )

    def _topology(self, declared: int, voices: list[DelegationResult], over_cap: bool) -> Topology:
        """The panel's observed process/agent fan-out (N1, item 3).

        ``declared`` is the intended width; ``realized_delegations`` is the subprocess delegations Rutherford
        launched, summed across the voices and INCLUDING any fallback re-runs (decision 3-A), so a fallback
        would show as realized > declared; ``observed_peak_agents`` is the max local descendant peak any voice
        sampled (a FLOOR -- remote agents are invisible -- and ``None`` when no voice was sampled, e.g. a fake
        that spawned nothing past the agent itself). ``over_cap`` flags a declared width over the advisory
        aggregate cap (informational unless ``enforce_agent_cap`` refused it up front).
        """
        observed = [v.observed_peak_agents for v in voices if v.observed_peak_agents is not None]
        realized = sum(v.delegation_call_count for v in voices)
        return Topology(
            declared=declared,
            realized_delegations=realized,
            observed_peak_agents=max(observed) if observed else None,
            over_cap=over_cap,
        )

    async def _fan_out(
        self,
        req: ConsensusRequest,
        targets: list[Target],
        budget: float | None,
        base_depth: int,
        on_activity: ActivityCallback | None,
        parent_run_id: str | None,
    ) -> tuple[list[DelegationResult], set[int], str | None, float]:
        """Run every voice, returning ``(voices, cut, stop_reason, elapsed_s)``, budget-aware.

        Without a budget (or with ``on_budget="continue"``, where the budget is advisory and every voice runs
        to completion) this is the plain parallel fan-out and ``stop_reason`` is ``None`` (a clean finish). With
        a binding budget it owns one :class:`~rutherford.acp.session.ACPSession` per voice, races them under an
        :func:`asyncio.wait` deadline, cuts the ones still in flight (harvesting each cut voice's streamed
        partial), and returns ``stop_reason="budget"`` with the cut indices. ``parent_run_id`` (when the panel
        persists) makes each un-budgeted voice persist as a child of the panel parent (F2).
        """
        on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
        if budget is None or on_budget == "continue":
            voices = list(
                await asyncio.gather(
                    *(
                        self._delegate_voice(req, i, t, base_depth, on_activity, parent_run_id)
                        for i, t in enumerate(targets)
                    )
                )
            )
            return voices, set(), None, 0.0
        return await self._fan_out_budgeted(req, targets, budget, base_depth, on_activity)

    async def _delegate_voice(
        self,
        req: ConsensusRequest,
        index: int,
        target: Target,
        base_depth: int,
        on_activity: ActivityCallback | None,
        parent_run_id: str | None,
    ) -> DelegationResult:
        """Run one voice through the delegation primitive (the un-budgeted / continue path).

        The delegation emits this voice's own ``voice_started`` / ``voice_finished`` activity events under a
        stable per-voice correlation id, gates the ACP turn on the shared concurrency semaphore, and layers
        the lineage/depth env -- so the panel's fan-out is both bounded and visible. When the panel persists
        (``parent_run_id`` set), the voice is written as a child leaf record of the parent (F2).
        """
        request = DelegationRequest(
            target=target,
            prompt=self._voice_prompt(req, target, index),
            working_dir=req.working_dir,
            files=req.files,
            # A per-seat ``Target.role`` overrides the call-level role for just this voice.
            role=target.role or req.role,
            safety_mode=req.safety_mode,
            timeout_s=req.timeout_s,
            effort=req.effort,  # the panel's producer-effort cap flows to every voice (F8a)
            # When the panel persists, each voice is a child record under the parent (F2); when it does not,
            # the voice never self-persists (no orphan per-voice records).
            persist=parent_run_id is not None,
            parent_run_id=parent_run_id,
        )
        return await self._delegation.delegate(
            request, correlation_id=f"voice:{index}", base_depth=base_depth, on_activity=on_activity
        )

    async def _fan_out_budgeted(
        self,
        req: ConsensusRequest,
        targets: list[Target],
        budget: float,
        base_depth: int,
        on_activity: ActivityCallback | None,
    ) -> tuple[list[DelegationResult], set[int], str | None, float]:
        """Race the voices under a wall-clock deadline; cut the stragglers and harvest their partials (F8a)."""
        cwd = req.working_dir or str(Path.cwd())
        policy = PermissionPolicy(mode=req.safety_mode)
        timeout_s = req.timeout_s or self._config.default_timeout_s
        sessions = [
            ACPSession(
                self._descriptors.get(target.cli),
                policy=policy,
                cwd=cwd,
                model=target.model,
                effort=req.effort,
                base_depth=base_depth,
            )
            if self._descriptors.has(target.cli)
            else None
            for target in targets
        ]
        start = time.monotonic()
        tasks = [
            asyncio.create_task(
                self._budget_turn(req, index, target, sessions[index], timeout_s, base_depth, on_activity)
            )
            for index, target in enumerate(targets)
        ]
        cut: set[int] = set()
        try:
            _done, pending = await asyncio.wait(tasks, timeout=budget)
            if pending:
                emit_activity(
                    on_activity,
                    ActivityEvent(
                        kind=ActivityEventKind.BUDGET_TICK,
                        tool="consensus",
                        depth=base_depth,
                        budget_left_s=0.0,
                        message=f"time budget ({budget:.0f}s) reached; harvesting {len(pending)} voice(s)",
                    ),
                )
                for index, task in enumerate(tasks):
                    if task in pending:
                        cut.add(index)
                        task.cancel()
                        emit_activity(
                            on_activity,
                            ActivityEvent(
                                kind=ActivityEventKind.CUT,
                                correlation_id=f"voice:{index}",  # collapses onto this voice's row
                                tool="consensus",
                                cli=targets[index].cli,
                                model=targets[index].model,
                                role=targets[index].role or req.role,
                                depth=base_depth,
                                status="cut",
                                message=f"{targets[index].display_label} cut at the time budget",
                            ),
                        )
                # Mandatory cancel-then-drain: only once each cancel is awaited does the ACP session tear down
                # its agent's process tree, so the cut voices are not left running past the deadline.
                await asyncio.gather(*pending, return_exceptions=True)
            voices = [
                self._collect_voice(req, index, targets[index], sessions[index], task, index in cut)
                for index, task in enumerate(tasks)
            ]
        finally:
            await asyncio.gather(*(s.close() for s in sessions if s is not None), return_exceptions=True)
        elapsed_s = round(time.monotonic() - start, 3)
        stop_reason = "budget" if cut else None  # None = a clean finish within the budget
        return voices, cut, stop_reason, elapsed_s

    async def _budget_turn(
        self,
        req: ConsensusRequest,
        index: int,
        target: Target,
        session: ACPSession | None,
        timeout_s: float,
        base_depth: int,
        on_activity: ActivityCallback | None,
    ) -> DelegationResult:
        """Open a voice's session and run its one turn; an unknown agent or handshake failure is a failed voice.

        Held by a task the budget loop may cancel mid-turn; on a cut, the harvested partial is read from the
        live session by :meth:`_collect_voice`, so this method itself never needs to swallow the cancel. The
        turn is gated on the shared concurrency semaphore (so a wide budgeted panel still honors
        ``max_concurrency``) and emits its own ``voice_started`` / ``voice_finished`` so it appears in the
        live activity stream like a non-budgeted voice.
        """
        if session is None:
            known = ", ".join(self._descriptors.ids()) or "(none)"
            return _fail_voice(
                target, req, ErrorCode.UNKNOWN_TARGET, f"unknown agent id {target.cli!r}; known: {known}"
            )
        try:
            await session.open()
        except ACPHandshakeError as exc:
            result = _fail_voice(target, req, exc.code, exc.message)
            result.effort = req.effort
            result.effort_applied = session.effort_applied
            self._emit_voice_finished(on_activity, index, result, target, req, base_depth)
            return result
        async with self._delegation.semaphore:
            emit_activity(
                on_activity,
                ActivityEvent(
                    kind=ActivityEventKind.VOICE_STARTED,
                    correlation_id=f"voice:{index}",
                    cli=target.cli,
                    model=target.model,
                    role=target.role or req.role,
                    depth=base_depth,
                    status="started",
                    message=f"{target.display_label} started",
                ),
            )
            result = await session.prompt(self._voice_prompt(req, target, index), timeout_s=timeout_s)
        self._emit_voice_finished(on_activity, index, result, target, req, base_depth)
        return result

    def _emit_voice_finished(
        self,
        on_activity: ActivityCallback | None,
        index: int,
        result: DelegationResult,
        target: Target,
        req: ConsensusRequest,
        base_depth: int,
    ) -> None:
        """Emit a budgeted voice's terminal ``voice_finished`` (a cut voice's terminal CUT is emitted by the loop)."""
        status = "ok" if result.ok else "failed"
        emit_activity(
            on_activity,
            ActivityEvent(
                kind=ActivityEventKind.VOICE_FINISHED,
                correlation_id=f"voice:{index}",
                cli=target.cli,
                model=result.target.model,
                role=target.role or req.role,
                status=status,
                elapsed_s=result.duration_s,
                observed_agents=result.observed_peak_agents,
                depth=base_depth,
                message=f"{target.display_label} {status}",
            ),
        )

    def _collect_voice(
        self,
        req: ConsensusRequest,
        index: int,
        target: Target,
        session: ACPSession | None,
        task: asyncio.Task[DelegationResult],
        was_cut: bool,
    ) -> DelegationResult:
        """Project one finished-or-cut task into a voice; a cut voice harvests its session's streamed partial."""
        if was_cut:
            return self._harvest_cut(req, target, session)
        if task.cancelled():  # an external cancel we did not induce -- propagate it
            raise asyncio.CancelledError()
        exc = task.exception()
        if exc is not None:
            raise exc
        return task.result()

    def _harvest_cut(self, req: ConsensusRequest, target: Target, session: ACPSession | None) -> DelegationResult:
        """Build the voice for a target cut at the deadline, keeping any answer text it streamed (F8a, 2-F).

        A non-empty partial is promoted to a usable answer (consensus, unlike a debate rebuttal, can use a
        cut voice's best-so-far): ``ok=True`` + ``stop_reason="budget"``. An empty partial is an honest failed
        voice with the cut recorded -- it stays in the panel and the denominator, never silently dropped.
        """
        partial = session.partial_text.strip() if session is not None else ""
        effort = self._delegation.resolve_effort(target.cli, req.effort)
        applied = session.effort_applied if session is not None else None
        # N1 (item 3): a cut voice still spun up a subprocess, so it counts 1 toward realized fan-out and
        # carries the peak the session's sampler observed before the cut (a floor) into the panel topology.
        observed = session.observed_peak_agents if session is not None else None
        if partial:
            return DelegationResult(
                target=session.target if session is not None else Target(cli=target.cli, model=target.model),
                ok=True,
                text=partial,
                stop_reason="budget",
                partial=partial,
                effort=effort,
                effort_applied=applied,
                safety_mode=req.safety_mode,
                observed_peak_agents=observed,
                provenance=Provenance(
                    provider=self._descriptors.get(target.cli).provider if self._descriptors.has(target.cli) else None,
                    model=session.target.model if session is not None else target.model,
                    confirmed=False,
                ),
            )
        return DelegationResult(
            target=session.target if session is not None else Target(cli=target.cli, model=target.model),
            ok=False,
            error=ErrorInfo(
                code=ErrorCode.BUDGET_EXHAUSTED,
                message=f"{target.cli} was cut at the {req.time_budget_s or self._config.default_time_budget_s:.0f}s "
                "time budget before it produced an answer",
            ),
            stop_reason="budget",
            effort=effort,
            effort_applied=applied,
            safety_mode=req.safety_mode,
            observed_peak_agents=observed,
        )

    def _rollup(
        self,
        req: ConsensusRequest,
        voices: list[DelegationResult],
        cut: set[int],
        budget: float | None,
        stop_reason: str | None,
        elapsed_s: float,
    ) -> RunRollup:
        """Summarize a time-budgeted consensus run into its :class:`RunRollup` (F8a).

        ``answered`` is every voice not cut at the deadline; ``usable`` is the answered (or harvested) voices
        with a non-empty answer. The rollup's ``stop_reason`` is the budget vocabulary -- ``"budget"`` for a
        harvest, ``"ok"`` for a clean finish within the budget (the result-level ``stop_reason`` stays ``None``
        on a clean finish). ``effort_requested`` / ``effort_applied`` are the highest tiers across the voices
        (the panel ran them all at one tier, but a per-agent default could differ), so the rollup shows what
        the budget actually bought.
        """
        requested = len(voices)
        cut_count = len(cut)
        answered = requested - cut_count
        usable = sum(1 for voice in voices if voice.ok and voice.text.strip())
        applied = [voice.effort_applied for voice in voices if voice.effort_applied is not None]
        effort_applied = max(applied, key=EFFORT_ORDER.index) if applied else None
        requested_tiers = [self._delegation.resolve_effort(voice.target.cli, req.effort) for voice in voices]
        present = [tier for tier in requested_tiers if tier is not None]
        effort_requested = max(present, key=EFFORT_ORDER.index) if present else None
        return RunRollup(
            stop_reason=stop_reason or "ok",
            requested=requested,
            answered=answered,
            cut=cut_count,
            usable=usable,
            quorum_met=usable >= self._config.min_quorum,
            elapsed_s=elapsed_s,
            time_budget_s=budget,
            effort_requested=effort_requested,
            effort_applied=effort_applied,
            cost=_sum_cost(voices),
        )

    def _resolve_targets(self, req: ConsensusRequest) -> tuple[list[Target], list[SkippedTarget]]:
        """Pick the panel's targets: the auto-expanded set, or the validated explicit list."""
        if req.expand_all:
            if req.stances is not None:
                raise RutherfordError(
                    ErrorCode.INVALID_INPUT,
                    "stances cannot be combined with an auto-expanded panel; name targets explicitly to steer them",
                )
            return self._expand_all()

        if not req.targets:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                "consensus needs at least one target, or set expand_all to fan out to every registered agent",
            )
        if len(req.targets) > self._config.max_targets:
            raise RutherfordError(
                ErrorCode.TOO_MANY_TARGETS,
                f"consensus requested {len(req.targets)} targets; the per-call cap is {self._config.max_targets}",
            )
        if req.stances is not None and len(req.stances) != len(req.targets):
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"stances ({len(req.stances)}) must match targets ({len(req.targets)})",
            )
        return list(req.targets), []

    def _expand_all(self) -> tuple[list[Target], list[SkippedTarget]]:
        """Build a full panel from every registered agent, capped at ``max_targets``.

        This phase fans out to every registered descriptor at its default model -- a genuinely
        unavailable agent surfaces as a failed voice rather than being pre-filtered (a live doctor probe
        per agent is a later refinement). A BENCHED agent (on cooldown, F7) is left OUT -- auto-selection
        should not keep reaching for a seat that just flapped -- and recorded in ``skipped`` with the time
        remaining. Any agent past the ``max_targets`` cap is also recorded in ``skipped`` with its reason, so
        the full attempted panel is visible.
        """
        included: list[Target] = []
        skipped: list[SkippedTarget] = []
        for descriptor in self._descriptors.all():
            if self._cooldown.is_benched(descriptor.id):
                remaining = self._cooldown.remaining_s(descriptor.id)
                skipped.append(SkippedTarget(cli=descriptor.id, reason=f"benched, {remaining:.0f}s remaining"))
                continue
            if len(included) >= self._config.max_targets:
                skipped.append(
                    SkippedTarget(cli=descriptor.id, reason=f"over max_targets ({self._config.max_targets})")
                )
                continue
            included.append(Target(cli=descriptor.id, model=None))
        return included, skipped

    def _voice_prompt(self, req: ConsensusRequest, target: Target, index: int) -> str:
        """The prompt for one voice: the question, stance-steered, plus a verdict ask under a tally strategy.

        RANK's first round is a plain answer (no verdict line) -- the ranking ask comes in its second round
        once every answer is in -- so only the one-shot tally strategies append the verdict instruction.
        """
        prompt = apply_stance(req.prompt, _stance_for(target, req.stances, index))
        if req.strategy not in (Strategy.ALL_VOICES, Strategy.RANK):
            prompt = f"{prompt}\n\n{verdict_instruction(req.verdict_schema)}"
        return prompt

    def _aggregate(
        self,
        req: ConsensusRequest,
        targets: list[Target],
        voices: list[DelegationResult],
        skipped: list[SkippedTarget],
    ) -> StrategyResult:
        """Extract each voice's verdict and reduce the panel to one outcome under ``req.strategy``.

        The per-seat metadata (``label`` / ``weight`` / ``parity``) is read from the original panel
        ``seat``, not from ``voice.target`` -- the ACP turn rebuilds the result's ``target`` as a bare
        ``(cli, model)`` pair, so the seat is the only place the steering survives. The resolved ``model``
        and ``provenance`` come from the voice's result, so a model fallback is reflected.
        """
        verdicts: list[VoiceVerdict] = []
        for seat, voice in zip(targets, voices, strict=True):
            extracted = extract_verdict(voice.text, req.verdict_schema) if voice.ok else None
            if not voice.ok:
                reason: str | None = "failed"
            elif extracted is None:
                reason = "unparseable"
            else:
                reason = None
            verdicts.append(
                VoiceVerdict(
                    label=seat.display_label,
                    cli=seat.cli,
                    model=voice.target.model,
                    weight=seat.effective_weight,
                    parity=seat.is_parity,
                    ok=voice.ok,
                    verdict=extracted,
                    no_verdict_reason=reason,
                    text=voice.text,
                    provenance=voice.provenance,
                )
            )
        outcome, decision = aggregate(req.strategy, verdicts, min_quorum=self._config.min_quorum)
        _stamp_dissent(verdicts, outcome, decision)
        return StrategyResult(
            strategy=req.strategy,
            outcome=outcome,
            decision=decision,
            voices=verdicts,
            skipped=skipped,
            diversity=self._diversity(voices),
        )

    async def _rank(
        self,
        req: ConsensusRequest,
        targets: list[Target],
        voices: list[DelegationResult],
        skipped: list[SkippedTarget],
        base_depth: int,
        on_activity: ActivityCallback | None,
        budget: float | None,
        mono_start: float,
    ) -> StrategyResult:
        """RANK's second round (F4b): each answering voice ranks the OTHERS, Borda-aggregated into a leaderboard.

        Round 1 already collected the answers (``voices``). Here every answering voice is shown the other
        answers under shuffled anonymous labels (7-D) with its OWN answer withheld (7-E) and asked to rank
        them; the ballots are de-anonymized and reduced by :func:`rank_panel`. A panel with fewer than two
        answers (or below ``min_quorum``) cannot rank and is ``no_quorum``. The time budget, if any, spans
        both rounds: when it is already spent the ranking round is skipped (every ballot empty), so a RANK
        panel never overruns its budget by a whole second round.
        """
        candidates = self._rank_candidates(targets, voices)
        require_dissent = req.require_dissent or self._config.require_dissent
        if len(candidates) < 2 or len(candidates) < self._config.min_quorum:
            return StrategyResult(
                strategy=Strategy.RANK,
                outcome="no_quorum",
                decision=None,
                voices=self._rank_verdicts(targets, voices, candidates, {}, None, require_dissent),
                skipped=skipped,
                diversity=self._diversity(voices),
            )
        ballots = await self._collect_ballots(req, candidates, base_depth, on_activity, budget, mono_start)
        outcome, decision, report = rank_panel(
            [(cand.label, cand.cli) for cand in candidates], ballots, min_quorum=self._config.min_quorum
        )
        leaderboard = {entry.label: entry for entry in report.leaderboard}
        return StrategyResult(
            strategy=Strategy.RANK,
            outcome=outcome,
            decision=decision,
            voices=self._rank_verdicts(targets, voices, candidates, leaderboard, report, require_dissent),
            skipped=skipped,
            diversity=self._diversity(voices),
            rank=report,
        )

    def _rank_candidates(self, targets: list[Target], voices: list[DelegationResult]) -> list[_Candidate]:
        """The answering voices, as the candidates RANK ranks (failed round-1 voices are not candidates)."""
        answering = [(index, voice) for index, voice in enumerate(voices) if voice.ok and voice.text.strip()]
        labels = _unique_labels([targets[index].display_label for index, _ in answering])
        return [
            _Candidate(
                pos=pos,
                target_index=index,
                label=labels[pos],
                cli=targets[index].cli,
                model=voice.target.model,
                text=voice.text,
                provenance=voice.provenance,
            )
            for pos, (index, voice) in enumerate(answering)
        ]

    async def _collect_ballots(
        self,
        req: ConsensusRequest,
        candidates: list[_Candidate],
        base_depth: int,
        on_activity: ActivityCallback | None,
        budget: float | None,
        mono_start: float,
    ) -> list[tuple[str, list[str]]]:
        """Run the ranking turn for each candidate in parallel and de-anonymize each ballot to real labels.

        When a time budget already elapsed in round 1 the ranking round is skipped entirely (every ballot
        empty -> ``no_quorum``); otherwise the turns run to completion under each voice's own ``timeout_s``.
        A turn that fails or yields no parseable ranking becomes an empty ballot, counted (never silent) by
        :func:`rank_panel` as unparseable.
        """
        if budget is not None and budget - (time.monotonic() - mono_start) <= 0:
            return [(candidate.label, []) for candidate in candidates]
        plans = [self._ballot_plan(req, candidate, candidates) for candidate in candidates]
        texts = await asyncio.gather(
            *(self._ranking_turn(req, candidate, prompt, base_depth, on_activity) for candidate, prompt, _ in plans)
        )
        ballots: list[tuple[str, list[str]]] = []
        for (candidate, _prompt, anon_to_label), text in zip(plans, texts, strict=True):
            anon = extract_ranking(text, req.verdict_schema, list(anon_to_label.keys())) or []
            ballots.append((candidate.label, [anon_to_label[label] for label in anon]))
        return ballots

    def _ballot_plan(
        self, req: ConsensusRequest, voter: _Candidate, candidates: list[_Candidate]
    ) -> tuple[_Candidate, str, dict[str, str]]:
        """Build one voter's ranking prompt: the OTHER answers, anonymized + shuffled, and the anon->label map.

        Self-exclusion (7-E): the voter never sees or ranks its own answer. Anonymization + per-voter shuffle
        (7-D): the others are relabelled A/B/C in a freshly shuffled order, so neither identity nor position
        leaks the source. The returned map de-anonymizes the voter's ballot back to the real candidate labels.
        """
        others = [cand for cand in candidates if cand.pos != voter.pos]
        shuffled = list(others)
        self._rng.shuffle(shuffled)
        anon_labels = _anon_labels(len(shuffled))
        anon_to_label = {anon: cand.label for anon, cand in zip(anon_labels, shuffled, strict=True)}
        block = "\n\n".join(f"## {anon}\n{cand.text.strip()}" for anon, cand in zip(anon_labels, shuffled, strict=True))
        prompt = (
            "Several AI coding agents answered the same question. Below are the OTHER answers (not your "
            "own), anonymized. Read them and rank them by quality, best to worst.\n\n"
            f"Question:\n{req.prompt}\n\nAnswers:\n\n{block}\n\n{ranking_instruction(anon_labels, req.verdict_schema)}"
        )
        return voter, prompt, anon_to_label

    async def _ranking_turn(
        self,
        req: ConsensusRequest,
        voter: _Candidate,
        prompt: str,
        base_depth: int,
        on_activity: ActivityCallback | None,
    ) -> str:
        """Run one voter's ranking turn (read-only, one level deeper) and return its answer text, or ``""``.

        Emits this seat's ``voice_started`` / ``voice_finished`` under a ``rank:<pos>`` correlation id so the
        ranking round is visible in the live activity view (item 3, 7-F). A failed turn returns ``""`` -- an
        empty ballot, recorded as unparseable rather than aborting the round.
        """
        if not self._descriptors.has(voter.cli):
            return ""
        descriptor = self._descriptors.get(voter.cli)
        cwd = req.working_dir or str(Path.cwd())
        timeout_s = req.timeout_s or self._config.default_timeout_s
        emit_activity(
            on_activity,
            ActivityEvent(
                kind=ActivityEventKind.VOICE_STARTED,
                correlation_id=f"rank:{voter.pos}",
                cli=voter.cli,
                model=voter.model,
                role=req.role,
                depth=base_depth,
                status="started",
                message=f"{voter.label} ranking started",
            ),
        )
        async with self._delegation.semaphore:
            result = await run_acp_turn(
                descriptor,
                prompt,
                policy=PermissionPolicy(SafetyMode.READ_ONLY),
                cwd=cwd,
                timeout_s=timeout_s,
                model=voter.model,
                base_depth=base_depth + 1,
            )
        emit_activity(
            on_activity,
            ActivityEvent(
                kind=ActivityEventKind.VOICE_FINISHED,
                correlation_id=f"rank:{voter.pos}",
                cli=voter.cli,
                model=result.target.model,
                role=req.role,
                status="ok" if result.ok else "failed",
                elapsed_s=result.duration_s,
                depth=base_depth,
                message=f"{voter.label} ranking {'ok' if result.ok else 'failed'}",
            ),
        )
        return result.text if result.ok else ""

    def _rank_verdicts(
        self,
        targets: list[Target],
        voices: list[DelegationResult],
        candidates: list[_Candidate],
        leaderboard: dict[str, RankEntry],
        report: RankReport | None,
        require_dissent: bool,
    ) -> list[VoiceVerdict]:
        """Project the panel into RANK :class:`VoiceVerdict`s: each candidate's standing, each failure visible.

        An answering voice carries its leaderboard ``rank``; a voice that failed round 1 stays in the list
        with ``no_verdict_reason="failed"`` and no rank (it was never a candidate). When ``require_dissent``
        is set and a clear winner emerged, every non-winning candidate is stamped with its standing (7-G,
        reusing the F4a ``dissent`` field) so a losing position is surfaced, not buried in the matrix.
        """
        by_index = {cand.target_index: cand for cand in candidates}
        winner = report.winner if report is not None else None
        total = len(candidates)
        verdicts: list[VoiceVerdict] = []
        for index, voice in enumerate(voices):
            cand = by_index.get(index)
            if cand is None:
                verdicts.append(
                    VoiceVerdict(
                        label=targets[index].display_label,
                        cli=targets[index].cli,
                        model=voice.target.model,
                        ok=voice.ok,
                        no_verdict_reason="failed",
                        text=voice.text,
                        provenance=voice.provenance,
                    )
                )
                continue
            entry = leaderboard.get(cand.label)
            dissent: str | None = None
            if require_dissent and winner is not None and entry is not None and entry.rank > 1:
                dissent = (
                    f"ranked #{entry.rank} of {total} (mean rank {entry.mean_rank}); the panel ranked {winner!r} first"
                )
            verdicts.append(
                VoiceVerdict(
                    label=cand.label,
                    cli=cand.cli,
                    model=cand.model,
                    ok=True,
                    text=cand.text,
                    provenance=cand.provenance,
                    rank=entry.rank if entry is not None else None,
                    dissent=dissent,
                )
            )
        return verdicts

    def _diversity(self, voices: list[DelegationResult]) -> DiversityReport | None:
        """Effective model/provider diversity across the voices that ANSWERED (ok with non-empty text), or None.

        An ``ok`` voice with empty text contributed no opinion, so it is excluded from the lineage count --
        the same answered-voice predicate the budget harvest, synthesis, and the debate diversity use, so the
        ``answered_voices`` headline is consistent across paths and an empty success never inflates a lineage.
        """
        answered = [voice.provenance for voice in voices if voice.ok and voice.text.strip()]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

    async def _maybe_synthesize(
        self, req: ConsensusRequest, voices: list[DelegationResult], base_depth: int
    ) -> tuple[str | None, str | None, bool]:
        """Resolve the tri-state ``synthesize`` and run a combining pass when it is on (``all-voices`` only).

        ``None`` means the caller omitted it -- the one case the configured ``synthesize_default`` fills;
        an explicit ``False`` always wins over a ``synthesize_default=true``. Returns ``(synthesis, label,
        self_authored)``.
        """
        effective = req.synthesize if req.synthesize is not None else self._config.synthesize_default
        if not effective or not voices:
            return None, None, False
        return await self._synthesize(req, voices, base_depth)

    async def _synthesize(
        self, req: ConsensusRequest, voices: list[DelegationResult], base_depth: int
    ) -> tuple[str | None, str | None, bool]:
        """Delegate a combining pass to the nominated judge, else the first successful voice.

        Mirrors the debate ``_synthesize`` pattern: a fresh one-shot ACP turn on a read-only session.
        Returns ``(synthesis, synthesizer_label, self_authored)``, or ``(None, None, False)`` when no
        synthesis was produced -- no successful voice, an unknown judge, or the synthesis run itself failed --
        so ``synthesis_by`` never names an author for a synthesis that does not exist.

        F4a no-self-approval (4-A): ``self_authored`` is ``True`` when the resolved judge is a panel
        participant (its CLI is one of the answering voices'); the default judge (the first voice) always is.
        When ``require_independent_judge`` is set (per call or config), a self-authored synthesis is REFUSED
        with ``INVALID_INPUT`` rather than silently authored -- name a non-participant ``judge``.
        """
        ok_voices = [voice for voice in voices if voice.ok and voice.text.strip()]
        if not ok_voices:
            return None, None, False
        first = ok_voices[0].target
        judge = req.judge if req.judge is not None else Target(cli=first.cli, model=first.model)
        # The judge is a participant iff its CLI is one of the answering voices' -- the agent is the unit of
        # independence (a same-CLI judge is "the defendant authoring the verdict" even on a different model).
        self_authored = judge.cli in {voice.target.cli for voice in ok_voices}
        if self_authored and (req.require_independent_judge or self._config.require_independent_judge):
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"require_independent_judge is set, but the synthesis would be authored by panel participant "
                f"{judge.cli!r}; name a non-participant judge (judge=<cli>) for a binding verdict",
            )
        if not self._descriptors.has(judge.cli):
            return None, None, False
        transcript = "\n\n".join(
            f"## {voice.target.cli}" + (f" ({voice.target.model})" if voice.target.model else "") + f"\n{voice.text}"
            for voice in ok_voices
        )
        prompt = (
            "You are synthesizing answers several AI coding agents gave to the same question.\n\n"
            f"Original question:\n{req.prompt}\n\n"
            f"Answers:\n\n{transcript}\n\n"
            "Write one synthesized answer: state where they agree, and where they disagree NAME each "
            "dissenting position you set aside and give a one-line reason it did not carry (F4a "
            "no-silent-dismissal). End with your best combined recommendation."
        )
        cwd = req.working_dir or str(Path.cwd())
        descriptor = self._descriptors.get(judge.cli)
        timeout_s = req.timeout_s or self._config.default_timeout_s
        # The synthesis is a nested delegation, so it runs one level deeper -- a Rutherford-host judge stays
        # bounded by the depth guard rather than being treated as a fresh top-level call.
        result = await run_acp_turn(
            descriptor,
            prompt,
            policy=PermissionPolicy(SafetyMode.READ_ONLY),
            cwd=cwd,
            timeout_s=timeout_s,
            model=judge.model,
            base_depth=base_depth + 1,
        )
        if not result.ok or not result.text.strip():
            return None, None, False
        return result.text, judge.display_label, self_authored


def _stamp_dissent(verdicts: list[VoiceVerdict], outcome: str, decision: str | None) -> None:
    """F4a no-silent-dismissal (4-B): stamp a structural reason on each PARSEABLE verdict that LOST.

    A voice with a real verdict that is not the panel's ``decision`` was set aside -- record why, so a
    losing-but-valid position is never silently dropped. ``no_verdict_reason`` stays distinct (it marks a
    voice that had NO verdict: failed / unparseable); ``dissent`` marks a verdict that lost. Only stamped when
    a decision was reached -- a split / tied / no_majority / no_quorum outcome has no winner to dissent from.
    The count is the head count of that verdict among the eligible (parseable) voices, so a weighted decision
    that overrode the head count reads honestly ("2 of 5 voted 'no'; the panel majority 'yes'").
    """
    if decision is None:
        return
    eligible = [v.verdict for v in verdicts if v.verdict is not None]
    tally = Counter(eligible)
    for verdict in verdicts:
        if verdict.verdict is not None and verdict.verdict != decision:
            verdict.dissent = (
                f"minority: {tally[verdict.verdict]} of {len(eligible)} voted {verdict.verdict!r}; "
                f"the panel {outcome} {decision!r}"
            )


def _unique_labels(labels: list[str]) -> list[str]:
    """Suffix ``#n`` to repeated display labels so two same-(cli, model) answers stay distinct on the leaderboard."""
    duplicated = {label for label in labels if labels.count(label) > 1}
    seen: dict[str, int] = {}
    out: list[str] = []
    for label in labels:
        if label in duplicated:
            seen[label] = seen.get(label, 0) + 1
            out.append(f"{label}#{seen[label]}")
        else:
            out.append(label)
    return out


def _anon_labels(count: int) -> list[str]:
    """``count`` single-letter anonymous ballot labels (A, B, ...), falling back to ``L<k>`` past 26."""
    return [string.ascii_uppercase[index] if index < 26 else f"L{index + 1}" for index in range(count)]


def _fail_voice(target: Target, req: ConsensusRequest, code: ErrorCode, message: str) -> DelegationResult:
    """A failed voice from an up-front guard (unknown agent, handshake failure) in the budgeted path."""
    return DelegationResult(
        target=Target(cli=target.cli, model=target.model),
        ok=False,
        error=ErrorInfo(code=code, message=message),
        safety_mode=req.safety_mode,
    )


def _sum_cost(voices: list[DelegationResult]) -> Cost | None:
    """Sum token usage across the answering voices, or ``None`` when no voice reported any (F8a rollup)."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    saw_any = False
    for voice in voices:
        if voice.cost is None:
            continue
        saw_any = True
        for field in totals:
            value = getattr(voice.cost, field)
            if value is not None:
                totals[field] += value
    if not saw_any:
        return None
    return Cost(**{field: value or None for field, value in totals.items()})


def _stance_for(target: Target, stances: list[Stance] | None, index: int) -> Stance | None:
    """The stance steering a voice: the target's own stance, else the parallel ``stances`` entry."""
    if target.stance is not None:
        return target.stance
    return stances[index] if stances else None


def _panel_voice(voice: DelegationResult) -> PanelVoice:
    """Project one consensus voice into the panel-parent's :class:`PanelVoice` summary (status + child link)."""
    return PanelVoice(
        label=voice.target.display_label,
        ok=voice.ok,
        run_id=Path(voice.run_dir).name if voice.run_dir else None,
        text=voice.text,
        error=voice.error.message if voice.error else None,
        cost=voice.cost,
        changed_files=tuple(voice.changed_files or []),
        partial=voice.partial,
        session_id=voice.session_id,
    )
