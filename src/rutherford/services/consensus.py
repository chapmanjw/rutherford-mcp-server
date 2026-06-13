# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The consensus service: the same prompt asked of several targets in parallel.

Returns every voice (one :class:`DelegationResult` per target) so the orchestrator can synthesize
them. Optional per-target stance steering nudges each voice for, against, or neutral. Optional
server-side synthesis (off by default) delegates a combining pass to one of the successful voices.
One failing voice never aborts the panel -- each target's failure is its own structured result.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..adapters.base import CLIAdapter
from ..adapters.registry import AdapterRegistry
from ..config.schema import RutherfordConfig
from ..domain.enums import EFFORT_ORDER, ActivityEventKind, AuthState, Effort, SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    ActivityEvent,
    AuthStatus,
    ConsensusRequest,
    ConsensusResult,
    Cost,
    DelegationRequest,
    DelegationResult,
    DetectResult,
    DiversityReport,
    ErrorInfo,
    InvocationContext,
    PanelInputs,
    PanelTarget,
    ProcessResult,
    RunRollup,
    SkippedTarget,
    StrategyResult,
    Target,
    Topology,
    VoiceVerdict,
)
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_aggregate_cap, ensure_within_target_cap
from .delegation import ActivityCallback, DelegationService, PanelLifecycle, ProgressCallback, emit_activity
from .persistence import PanelVoice, live_tee, render_panel_voice_files, stop_live_tee, write_panel_record
from .strategies import aggregate, apply_stance, effective_diversity, extract_verdict, verdict_instruction


class ConsensusService:
    """Runs a consensus panel across targets, with optional stances and synthesis."""

    def __init__(
        self,
        delegation: DelegationService,
        config: RutherfordConfig,
        registry: AdapterRegistry,
        *,
        ledger: RunLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._delegation = delegation
        self._config = config
        self._registry = registry
        #: The durable run ledger (F2) for the panel's parent record; ``None`` disables persistence.
        self._ledger = ledger
        self._clock = clock

    async def consensus(
        self,
        req: ConsensusRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
        on_activity: ActivityCallback | None = None,
        on_interim_result: Callable[[ConsensusResult | StrategyResult], None] | None = None,
    ) -> ConsensusResult | StrategyResult:
        """Fan out ``req`` across its targets and collect every voice.

        With ``expand_all``, the panel is built from every installed + authenticated adapter
        (see :meth:`_expand_all`); otherwise it is the explicit ``targets``. A failing target is
        its own structured voice, so one bad voice never aborts the panel. With a ``strategy`` other
        than ``all-voices``, the voices are aggregated into a :class:`StrategyResult`; otherwise the
        legacy :class:`ConsensusResult` (every voice, plus optional synthesis) is returned unchanged.

        ``on_interim_result`` implements ``on_budget=continue`` (F8a, 2-M): when set (an async job) and a
        time budget is in effect, at the deadline the panel publishes the best-effort answered-so-far set
        through this sink and keeps the stragglers running, publishing an updated set as each lands -- so a
        poller sees partial answers before the panel finishes -- then returns the full set. Without it (a
        sync call), ``continue`` simply runs every voice to completion.
        """
        # N1 (3-K): wrap the whole panel run so a cancellation at ANY of its awaits (voice waits, the
        # live-tee stop, active harvest, the closing synthesis, the record persist) closes the activity
        # stream with exactly one terminal event. The lifecycle emits panel_started/finished from inside the
        # body; here a cancel emits job_cancelled iff the panel started and has not already closed.
        lifecycle = PanelLifecycle("consensus", base_depth, on_activity)
        try:
            return await self._consensus_impl(
                req,
                lifecycle,
                correlation_id=correlation_id,
                base_depth=base_depth,
                on_progress=on_progress,
                on_activity=on_activity,
                on_interim_result=on_interim_result,
            )
        except asyncio.CancelledError:
            lifecycle.on_cancel()
            raise

    async def _consensus_impl(
        self,
        req: ConsensusRequest,
        lifecycle: PanelLifecycle,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
        on_activity: ActivityCallback | None = None,
        on_interim_result: Callable[[ConsensusResult | StrategyResult], None] | None = None,
    ) -> ConsensusResult | StrategyResult:
        """The consensus panel body; the public :meth:`consensus` wraps this with the lifecycle guard."""
        created_at = self._clock()
        persist = self._config.wants_persist(req.persist)
        parent_run_id = uuid.uuid4().hex if persist and self._ledger is not None else None
        targets, skipped = await self._resolve_targets(req, on_progress)

        # N1 (item 3): the declared fan-out width. Check it against the advisory aggregate-agent cap up
        # front (a no-op unless one is configured; refuses only when ``enforce_agent_cap`` is also set), and
        # announce the panel as started so a sync caller is pushed the fan-out total before the voices run.
        declared_width = len(targets)
        ensure_within_aggregate_cap(
            declared_width, self._config.max_agents_advisory, enforce=self._config.enforce_agent_cap
        )
        lifecycle.mark_started(
            ActivityEvent(
                kind=ActivityEventKind.PANEL_STARTED,
                tool="consensus",
                depth=base_depth,
                declared=declared_width,
                message=f"consensus panel started: {declared_width} voice(s)",
            )
        )

        requests = [
            DelegationRequest(
                target=target,
                prompt=self._voice_prompt(req, target, index),
                working_dir=req.working_dir,
                files=req.files,
                role=target.role or req.role,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
                effort=req.effort,  # the panel's producer-effort cap flows to every voice (F8a)
                include_raw=req.include_raw,
                # When the panel persists, each voice is a child record under the parent (F2); when it
                # does not, the voice never self-persists (no orphan per-voice records).
                persist=parent_run_id is not None,
                parent_run_id=parent_run_id,
            )
            for index, target in enumerate(targets)
        ]
        # Per-voice stdout accumulators (F8a, 2-F): a voice cut at the time-budget deadline can still
        # surface the partial answer it streamed before the cut, on both the job and ephemeral paths.
        partials: list[list[str]] = [[] for _ in requests]

        async def one(index: int, target_request: DelegationRequest) -> DelegationResult:
            return await self._delegation.delegate(
                target_request,
                correlation_id=f"{correlation_id}:{index}",
                base_depth=base_depth,
                on_progress=on_progress,
                on_stdout=partials[index].append,
                on_activity=on_activity,  # N1: each voice emits its own voice_started/voice_finished
            )

        # Time-budget harvest (F8a, 2-A'/2-behavior/2-where): run the voices as tasks under an
        # asyncio.wait deadline; at the deadline keep the answered voices and cut the in-flight ones.
        tasks = [asyncio.create_task(one(i, r)) for i, r in enumerate(requests)]
        budget = req.time_budget_s if req.time_budget_s is not None else self._config.default_time_budget_s
        cut: set[int] = set()
        stop_reason: str | None = None
        # Resolve the disposition: the call value, else the configured ``default_on_budget`` (2-M's
        # per-call-param + workspace-default). ``continue`` makes the budget advisory -- run every voice to
        # completion (nothing is cut). ``harvest`` (the default) and ``resume`` both cut the stragglers at
        # the deadline; ``resume`` is equivalent to ``harvest`` today (a voice cut mid-run has no established
        # session to record, and the deliberate come-back rides the item-9 continuation primitive). OnBudget.
        on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
        enforce = bool(tasks) and budget is not None and on_budget != "continue"
        # ``on_budget=continue`` with an interim sink (an async job, 2-M): detach at the deadline -- publish
        # the best-effort answered-so-far set and keep the stragglers running, appending as each lands --
        # rather than cut them (harvest) or block until all finish (the sync continue).
        continue_detach = (
            bool(tasks) and budget is not None and on_budget == "continue" and on_interim_result is not None
        )
        # Stream-to-job (F8a, 2-G): while a persisted panel runs, tee each voice's accumulating stdout into
        # the job's artifacts off-thread on a coarse timer, so a kept job preserves the in-flight work up to
        # a crash or a cut -- not just what survives to finalization. No-op for an ephemeral run (in-memory).
        tee_run_id = parent_run_id if (parent_run_id is not None and self._ledger is not None) else None
        tee_stop = asyncio.Event()
        tee_task = (
            asyncio.create_task(live_tee(self._ledger, tee_run_id, "voice", partials, tee_stop))
            if tee_run_id and self._ledger is not None
            else None
        )
        try:
            if enforce:
                _done, pending = await asyncio.wait(tasks, timeout=budget)
                if pending:
                    stop_reason = "budget"
                    # N1: one budget_tick at the deadline, then a cut event per voice being harvested.
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
                                    # The same per-voice correlation id its delegate used, so the cut collapses
                                    # onto that voice's row (the stable key, robust to a model fallback).
                                    correlation_id=f"{correlation_id}:{index}",
                                    tool="consensus",
                                    cli=requests[index].target.cli,
                                    model=requests[index].target.model,
                                    role=requests[index].role,
                                    depth=base_depth,
                                    status="cut",
                                    message=f"{requests[index].target.display_label} cut at the time budget",
                                ),
                            )
                    # MANDATORY cancel-then-drain: the runner kills the CLI process tree only once the
                    # cancellation is delivered AND awaited -- skipping this leaks orphaned subprocesses.
                    await asyncio.gather(*pending, return_exceptions=True)
            elif continue_detach:
                _done, pending = await asyncio.wait(tasks, timeout=budget)
                while pending:  # at/after the deadline: publish the set so far, then await the next straggler
                    assert on_interim_result is not None  # narrowed by continue_detach
                    on_interim_result(self._interim_result(req, requests, tasks, skipped))
                    _d, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # The OUTER coroutine was cancelled (e.g. job_cancel): asyncio.wait/gather do not cancel the
            # voice tasks for us, so cancel + drain them (no orphaned trees) before propagating. The terminal
            # job_cancelled activity event is emitted once by the panel-lifecycle guard in ``consensus``.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            # Stop the tee (it writes a final snapshot itself, then exits), so even a cancelled panel leaves
            # the live stream on disk. Runs on every exit (normal, budget harvest, or outer cancel).
            await stop_live_tee(tee_task, tee_stop)

        voices: list[DelegationResult] = []
        for index, (request, task) in enumerate(zip(requests, tasks, strict=True)):
            if task.cancelled():
                if index in cut:
                    voices.append(self._budget_voice(request, partials[index]))  # cut at the deadline
                    continue
                raise asyncio.CancelledError()  # an external cancel we did not induce -- propagate it
            exc = task.exception()
            if isinstance(exc, asyncio.CancelledError):
                raise exc
            if exc is not None:
                voices.append(_escaped_voice(request, exc))
            else:
                voices.append(task.result())

        # 2-I active resumable harvest: when opted in, re-prompt each cut voice whose session was recovered
        # for a clean best answer, BEFORE the quorum gate (the follow-up may turn a raw partial into a usable
        # answer). Runs only on a budget harvest with cut voices.
        if stop_reason == "budget" and req.harvest_partial and cut:
            await self._active_harvest(req, voices, cut, correlation_id, base_depth, on_activity)

        # 2-E': a budget harvest that yielded fewer than min_quorum usable voices is a genuine failure
        # (BUDGET_EXHAUSTED), raised before any result/persist; otherwise the harvest is a success.
        if stop_reason == "budget":
            usable = sum(1 for voice in voices if voice.ok and voice.text.strip())
            if usable < self._config.min_quorum:
                # N1 (3-K): this is a terminal outcome, so close the activity stream with a (failed)
                # panel_finished BEFORE raising -- otherwise the stream/push has no terminal event.
                lifecycle.mark_closed(
                    ActivityEvent(
                        kind=ActivityEventKind.PANEL_FINISHED,
                        tool="consensus",
                        depth=base_depth,
                        declared=declared_width,
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

        rollup = self._rollup(req, voices, cut, budget, stop_reason, self._clock() - created_at) if budget else None
        # N1 (item 3): the panel's observed fan-out -- declared width, the voice delegations launched, and
        # the local descendant peak across the voices (a floor). Attached to the result and persisted onto
        # the parent record so a kept run has its topology.
        topology = self._topology(declared_width, voices)

        # None means the caller omitted synthesize, the one case the configured default fills; an explicit
        # False wins over a synthesize_default=true config. Resolved here so the panel record snapshots the
        # *resolved* value (decision 1-D), not the unresolved request.
        effective_synthesize = req.synthesize if req.synthesize is not None else self._config.synthesize_default
        synthesis: str | None = None
        synthesis_by: str | None = None
        if req.strategy is Strategy.ALL_VOICES and effective_synthesize and voices:
            synthesis, synthesis_by = await self._synthesize(req, voices, correlation_id, base_depth)
        result, answer = self._assemble(req, voices, skipped, synthesis=synthesis, synthesis_by=synthesis_by)
        # Carry the harvest disposition onto the result: ``stop_reason`` flags a budget harvest, and the
        # rollup (only when a budget governed the run) reports requested/answered/cut/usable + effort (F8a).
        result.stop_reason = stop_reason
        result.rollup = rollup
        result.topology = topology  # N1: the observed fan-out

        if parent_run_id is not None and self._ledger is not None:
            # Write the parent panel record linking the child voice records (off-thread: file I/O). The
            # parent's status is derived from the voices, and a voices/voice-N.md per voice (plus a
            # skipped.md for an auto-panel's left-out adapters) makes the parent auditable without every
            # child record still on disk. The parent also rolls up the request's safety/files/role.
            panel_voices = [_panel_voice(voice) for voice in voices]
            skipped_pairs = [(entry.cli, entry.reason) for entry in skipped]
            panel_inputs = PanelInputs(
                targets=[
                    PanelTarget(
                        cli=voice.target.cli,
                        model=voice.target.model,
                        # The effective per-seat stance: the target's own, else the parallel stances entry
                        # (mirrors _voice_prompt), so a parallel-stances panel records who argued which side.
                        stance=_stance_for(voice.target, req.stances, index),
                        # The seat's resume handle in the parent state.toon (2-I) -- matters most for a cut
                        # voice, whose handle is recovered from the harvested partial and has no child record.
                        session_id=voice.session_id,
                    )
                    for index, voice in enumerate(voices)
                ],
                strategy=req.strategy.value,
                synthesize=effective_synthesize,
                judge=req.judge.display_label if req.judge else None,
            )
            result.run_dir = await asyncio.to_thread(
                write_panel_record,
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
        # N1 (3-K): the terminal panel_finished is the LAST step (no await follows), so the lifecycle guard
        # never has to choose between this and job_cancelled -- a cancel before here lands in the guard, a
        # clean run emits this and returns.
        lifecycle.mark_closed(
            ActivityEvent(
                kind=ActivityEventKind.PANEL_FINISHED,
                tool="consensus",
                depth=base_depth,
                declared=declared_width,
                done=sum(1 for voice in voices if voice.ok),
                observed_agents=topology.observed_peak_agents,
                message=f"consensus panel finished: {sum(1 for v in voices if v.ok)}/{len(voices)} ok",
            )
        )
        return result

    def _diversity(self, voices: list[DelegationResult]) -> DiversityReport | None:
        """Effective model/provider diversity across the voices that answered, or ``None`` if none did."""
        answered = [voice.provenance for voice in voices if voice.ok]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

    def _assemble(
        self,
        req: ConsensusRequest,
        voices: list[DelegationResult],
        skipped: list[SkippedTarget],
        *,
        synthesis: str | None = None,
        synthesis_by: str | None = None,
    ) -> tuple[ConsensusResult | StrategyResult, str]:
        """Build the panel result (and its answer text) from ``voices``: a :class:`StrategyResult` under a
        strategy, else a :class:`ConsensusResult`. Shared by the final result and the ``continue`` interim
        preview (the interim passes no synthesis -- it is a cheap snapshot, not the finished panel)."""
        if req.strategy is not Strategy.ALL_VOICES:
            result = self._aggregate(req, voices, skipped)
            return result, (result.decision or result.outcome)
        consensus_result = ConsensusResult(
            voices=voices,
            synthesis=synthesis,
            synthesis_by=synthesis_by,
            skipped=skipped,
            diversity=self._diversity(voices),
        )
        return consensus_result, (synthesis or "(no synthesis -- see the linked voice records)")

    def _interim_result(
        self,
        req: ConsensusRequest,
        requests: list[DelegationRequest],
        tasks: list[asyncio.Task[DelegationResult]],
        skipped: list[SkippedTarget],
    ) -> ConsensusResult | StrategyResult:
        """A best-effort preview of the panel from the voices that have finished so far (F8a, 2-M continue).

        Built from the currently-done tasks (a still-running or cancelled voice is omitted), with no
        synthesis (a cheap snapshot, not the finished panel) and a notice that the panel is still running.
        Published to the job's interim sink so a poller sees partial answers before the stragglers land.
        """
        voices: list[DelegationResult] = []
        for request, task in zip(requests, tasks, strict=True):
            if not task.done() or task.cancelled() or isinstance(task.exception(), asyncio.CancelledError):
                continue  # still running, or cancelled -- not part of the answered-so-far set
            exc = task.exception()
            voices.append(_escaped_voice(request, exc) if exc is not None else task.result())
        result, _answer = self._assemble(req, voices, skipped)
        result.notice = f"interim: {len(voices)} of {len(tasks)} voice(s) answered; the panel is still running"
        return result

    def _budget_voice(self, request: DelegationRequest, partial_lines: list[str]) -> DelegationResult:
        """The voice for a target cut at the panel's time-budget deadline (F8a, 2-E'/2-F/2-H).

        Don't waste the in-flight work: if the target's adapter ``supports_partial_output`` and it streamed
        something before the cut, the partial is harvested through the adapter's OWN ``parse_output`` -- the
        only thing that turns a JSONL/text stream into a clean candidate answer (and recovers any session
        handle for a later resume). A usable partial answer is returned ``ok=True`` with ``stop_reason``
        ``"budget"`` and the raw bytes on ``partial``, so it counts toward quorum and feeds the
        aggregation/synthesis. When there is no usable partial (a single-envelope adapter whose answer only
        arrives at the end, an empty stream, or a partial the adapter can't yet parse into an answer) the
        voice is the honest ``BUDGET_EXHAUSTED`` failure, with the raw partial preserved as a trace.
        """
        partial_text = "\n".join(partial_lines).strip()
        effort = self._delegation.resolve_effort(request.target.cli, request.effort)
        applied = self._delegation.applied_effort(request.target.cli, effort)
        parsed = self._parse_partial(request, partial_text, effort) if partial_text else None
        if parsed is not None and parsed.ok and parsed.text.strip():
            # A usable partial answer: keep it ``ok`` (it counts toward quorum) but mark it as a budget
            # harvest and preserve the raw bytes, so a reader can tell a partial answer from a complete one.
            parsed.stop_reason = "budget"
            parsed.partial = partial_text
            parsed.effort = effort
            parsed.effort_applied = applied
            return parsed
        # A trace-only cut still ran with the resolved effort, so report it (2-L-map: always report what was
        # enforced); and carry any session the partial established (even with no answer) so the cut voice can
        # be resumed later (2-I passive). ``parsed`` is the failed parse, which now carries that session.
        session = parsed.session_id if parsed is not None else None
        return DelegationResult(
            target=request.target,
            ok=False,
            error=ErrorInfo(code=ErrorCode.BUDGET_EXHAUSTED, message="cut at the panel time-budget deadline"),
            safety_mode=request.safety_mode,
            partial=partial_text or None,
            stop_reason="budget",
            session_id=session,
            effort=effort,
            effort_applied=applied,
        )

    def _parse_partial(
        self, request: DelegationRequest, partial_text: str, effort: Effort | None
    ) -> DelegationResult | None:
        """Run a cut voice's streamed partial through the adapter's ``parse_output``, or ``None`` (F8a, 2-H).

        Only for an adapter whose ``supports_partial_output`` is true (JSONL/text stream the answer as it is
        produced); single-envelope adapters emit the answer once at the end, so a cut yields only a trace.
        Returns the parsed result whether or not it is ``ok``: a clean answer where the stream already carried
        one, otherwise a failed parse that still carries any ``session_id`` the stream established (for 2-I
        passive resume). ``None`` when the adapter has no partial output or the parse raised. Never raises.
        """
        try:
            adapter = self._registry.get(request.target.cli)
            if not adapter.capabilities().supports_partial_output:
                return None
            ctx = InvocationContext(target=request.target, safety_mode=request.safety_mode, effort=effort)
            return adapter.parse_output(ProcessResult(exit_code=0, stdout=partial_text), ctx)
        except Exception:
            return None

    async def _active_harvest(
        self,
        req: ConsensusRequest,
        voices: list[DelegationResult],
        cut: set[int],
        correlation_id: str,
        base_depth: int,
        on_activity: ActivityCallback | None = None,
    ) -> None:
        """Re-prompt each cut voice whose session was recovered for a clean best answer (F8a, 2-I).

        ``harvest_partial=true`` only: a cut voice whose adapter supports resume and whose in-flight session
        the partial harvest recovered (``session_id``) gets a bounded "you're out of time, give your current
        best answer" follow-up against that session; its clean answer replaces the raw partial in ``voices``.
        The follow-ups run concurrently and best-effort -- a failed one leaves the original cut voice intact.
        The follow-up is not its own job record (it continues a panel voice). It spends budget you may be out
        of, which is why it is opt-in. ``on_activity`` is threaded so the follow-up appears in the live stream
        (decision 3-K): it reuses the cut voice's correlation id, so its start/finish collapse onto that
        voice's activity row (cut -> ok) rather than adding a phantom seat.
        """

        async def followup(index: int) -> None:
            voice = voices[index]
            if not voice.session_id:
                return  # no recovered session (single-envelope adapter, or nothing streamed) -- nothing to resume
            try:
                if not self._registry.get(voice.target.cli).capabilities().supports_resume:
                    return
            except Exception:
                return
            follow_req = DelegationRequest(
                target=voice.target,
                session_id=voice.session_id,
                prompt="You are out of time. Give your current best answer now, as concisely as you can.",
                working_dir=req.working_dir,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
                effort=voice.effort,
                persist=False,  # the follow-up continues a panel voice; it is not its own job record
            )
            result = await self._delegation.delegate(
                # Reuse the cut voice's correlation id so the follow-up's activity events collapse onto that
                # voice's row (cut -> ok) and the pusher counts the voice once, not as an extra seat.
                follow_req,
                correlation_id=f"{correlation_id}:{index}",
                base_depth=base_depth,
                on_activity=on_activity,
            )
            if result.ok and result.text.strip():
                result.stop_reason = "budget"  # a post-deadline harvested answer, not a clean in-budget finish
                # N1 (3-A): the follow-up is ANOTHER Rutherford delegation on top of the cut voice's own, so
                # carry the cut voice's count forward rather than dropping it when we replace the voice.
                result.delegation_call_count += voice.delegation_call_count
                voices[index] = result

        await asyncio.gather(*(followup(index) for index in sorted(cut)), return_exceptions=True)

    def _rollup(
        self,
        req: ConsensusRequest,
        voices: list[DelegationResult],
        cut: set[int],
        budget: float | None,
        stop_reason: str | None,
        elapsed_s: float,
    ) -> RunRollup:
        """Summarize a budget-governed panel: counts, quorum, the highest effort applied, and summed cost."""
        requested = len(voices)
        cut_count = len(cut)
        answered = requested - cut_count
        usable = sum(1 for voice in voices if voice.ok and voice.text.strip())
        applied = [voice.effort_applied for voice in voices if voice.effort_applied is not None]
        effort_applied = max(applied, key=EFFORT_ORDER.index) if applied else None
        # Report the RESOLVED requested effort, derived per seat so a per-adapter ``[adapters.<id>].effort``
        # default is reflected (not just the global default): the highest tier any voice actually requested.
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
            cost=_rollup_voice_cost(voices),
        )

    def _topology(self, declared_width: int, voices: list[DelegationResult]) -> Topology:
        """The panel's observed process/agent fan-out (N1, item 3).

        ``declared`` is the intended width; ``realized_delegations`` is the subprocess delegations Rutherford
        launched, summed across the voices and INCLUDING each voice's fallback re-runs (decision 3-A), so a
        model/cross-target fallback shows up as realized > declared; ``observed_peak_agents`` is the max local
        descendant peak across the voices (a FLOOR -- remote agents are invisible -- and ``None`` when no voice
        was sampled, e.g. a fake runner). ``over_cap`` flags a realized count over the advisory cap
        (informational unless ``enforce_agent_cap`` is on, which refuses on the declared width up front).
        """
        observed = [voice.observed_peak_agents for voice in voices if voice.observed_peak_agents is not None]
        realized = sum(voice.delegation_call_count for voice in voices)
        cap = self._config.max_agents_advisory
        return Topology(
            declared=declared_width,
            realized_delegations=realized,
            observed_peak_agents=max(observed) if observed else None,
            over_cap=cap is not None and realized > cap,
        )

    def _voice_prompt(self, req: ConsensusRequest, target: Target, index: int) -> str:
        """The prompt for one voice: the question, stance-steered, plus a verdict ask under a strategy."""
        prompt = apply_stance(req.prompt, _stance_for(target, req.stances, index))
        if req.strategy is not Strategy.ALL_VOICES:
            prompt = f"{prompt}\n\n{verdict_instruction(req.verdict_schema)}"
        return prompt

    def _aggregate(
        self,
        req: ConsensusRequest,
        voices: list[DelegationResult],
        skipped: list[SkippedTarget],
    ) -> StrategyResult:
        """Extract each voice's verdict and reduce the panel to one outcome under ``req.strategy``."""
        verdicts: list[VoiceVerdict] = []
        for voice in voices:
            extracted = extract_verdict(voice.text, req.verdict_schema) if voice.ok else None
            if not voice.ok:
                reason: str | None = "failed"
            elif extracted is None:
                reason = "unparseable"
            else:
                reason = None
            verdicts.append(
                VoiceVerdict(
                    label=voice.target.display_label,
                    cli=voice.target.cli,
                    model=voice.target.model,
                    weight=voice.target.effective_weight,
                    parity=voice.target.is_parity,
                    ok=voice.ok,
                    verdict=extracted,
                    no_verdict_reason=reason,
                    text=voice.text,
                    provenance=voice.provenance,
                    effort_applied=voice.effort_applied,  # carry the applied effort to the strategy view (F8a)
                )
            )
        outcome, decision = aggregate(req.strategy, verdicts, min_quorum=self._config.min_quorum)
        return StrategyResult(
            strategy=req.strategy,
            outcome=outcome,
            decision=decision,
            voices=verdicts,
            skipped=skipped,
            diversity=self._diversity(voices),
        )

    async def _resolve_targets(
        self,
        req: ConsensusRequest,
        on_progress: ProgressCallback | None,
    ) -> tuple[list[Target], list[SkippedTarget]]:
        """Pick the panel's targets: the auto-expanded set, or the validated explicit list."""
        if req.expand_all:
            if req.stances is not None:
                raise RutherfordError(
                    ErrorCode.INVALID_INPUT,
                    "stances cannot be combined with an auto-expanded panel; name targets explicitly to steer them",
                )
            return await self._expand_all(on_progress)

        if not req.targets:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                "consensus requires at least one target, or set expand_all to fan out to every authenticated CLI",
            )
        ensure_within_target_cap(len(req.targets), self._config.max_targets)
        if req.stances is not None and len(req.stances) != len(req.targets):
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"stances ({len(req.stances)}) must match targets ({len(req.targets)})",
            )
        return list(req.targets), []

    async def _expand_all(
        self,
        on_progress: ProgressCallback | None,
    ) -> tuple[list[Target], list[SkippedTarget]]:
        """Build a full panel from every adapter that is installed and not known-unauthenticated.

        Includes an adapter when ``detect`` reports installed and ``check_auth`` is authenticated
        or unknown. Unknown adapters (Antigravity, Qwen) have no cheap auth check -- doctor verifies
        them live -- so they are included optimistically; a genuinely unauthenticated one returns a
        failed voice rather than being silently dropped. Not-installed and definitively
        unauthenticated (needs_login / api_key_missing) adapters are skipped with a reason, as are
        any past the ``max_targets`` cap. **Optional adapters (a local model) are excluded from the
        auto panel** -- they only join when named explicitly, so the opt-in local model never silently
        slows an otherwise-cloud panel. Each included target uses the adapter's default model.
        """

        async def probe(adapter: CLIAdapter) -> tuple[DetectResult, AuthStatus | None]:
            """One adapter's probes off-thread: detect, then auth only if installed."""
            detected = await asyncio.to_thread(adapter.detect)
            if not detected.installed:
                return detected, None
            return detected, await asyncio.to_thread(adapter.check_auth)

        # Probes run concurrently: sequentially they cost up to two probe-subprocess latencies per
        # adapter cold, and one hung shim stalls panel assembly for everyone behind it. Membership
        # decisions stay a sequential pass in registry order below, so skip ordering and the
        # max_targets cap behave exactly as before. Optional adapters are never probed -- the
        # sequential code skipped them before detect, and the auto panel excludes them anyway.
        adapters = self._registry.all()
        candidates = [adapter for adapter in adapters if not adapter.optional]
        outcomes = await asyncio.gather(*(probe(adapter) for adapter in candidates))
        probed = {adapter.id: outcome for adapter, outcome in zip(candidates, outcomes, strict=True)}

        included: list[Target] = []
        skipped: list[SkippedTarget] = []
        for adapter in adapters:
            if adapter.optional:
                skipped.append(SkippedTarget(cli=adapter.id, reason="optional; name it explicitly to include"))
                continue
            detected, auth = probed[adapter.id]
            if not detected.installed:
                skipped.append(SkippedTarget(cli=adapter.id, reason="not installed or not on PATH"))
                continue
            if auth is not None and auth.state in (AuthState.NEEDS_LOGIN, AuthState.API_KEY_MISSING):
                reason = auth.detail or f"not authenticated ({auth.state.value})"
                skipped.append(SkippedTarget(cli=adapter.id, reason=reason))
                continue
            if self._delegation.is_benched(adapter.id):
                remaining = self._delegation.cooldown_remaining_s(adapter.id)
                skipped.append(SkippedTarget(cli=adapter.id, reason=f"on cooldown ({remaining:.0f}s remaining)"))
                continue
            if len(included) >= self._config.max_targets:
                skipped.append(SkippedTarget(cli=adapter.id, reason=f"over max_targets ({self._config.max_targets})"))
                continue
            included.append(Target(cli=adapter.id, model=None))

        _announce_panel(on_progress, included, skipped)
        return included, skipped

    async def _synthesize(
        self,
        req: ConsensusRequest,
        voices: list[DelegationResult],
        correlation_id: str,
        base_depth: int,
    ) -> tuple[str | None, str | None]:
        """Delegate a combining pass to the nominated judge, else the first successful voice.

        Returns ``(synthesis, synthesizer_label)``, or ``(None, None)`` when no synthesis was produced
        -- no successful voice, or the synthesis run itself failed -- so ``synthesis_by`` never names an
        author for a synthesis that does not exist. The label is the target that actually answered
        (reflecting any model fallback): a caller-named ``judge`` when given (ideally a non-participant),
        otherwise the first successful voice, which ``synthesis_by`` makes visible rather than hidden.
        """
        ok_voices = [voice for voice in voices if voice.ok and voice.text.strip()]
        if not ok_voices:
            return None, None
        transcript = "\n\n".join(
            f"## {voice.target.cli}" + (f" ({voice.target.model})" if voice.target.model else "") + f"\n{voice.text}"
            for voice in ok_voices
        )
        prompt = (
            "You are synthesizing answers several AI coding agents gave to the same question.\n\n"
            f"Original question:\n{req.prompt}\n\n"
            f"Answers:\n\n{transcript}\n\n"
            "Write one synthesized answer: state where they agree, flag where they disagree, and give "
            "your best combined recommendation."
        )
        judge_target = req.judge or ok_voices[0].target
        synth_request = DelegationRequest(
            target=judge_target,
            prompt=prompt,
            working_dir=req.working_dir,
            safety_mode=SafetyMode.READ_ONLY,
            timeout_s=req.timeout_s,
            persist=False,  # the synthesis pass is internal; it never becomes its own job record (F2)
        )
        result = await self._delegation.delegate(
            synth_request,
            correlation_id=f"{correlation_id}:synthesis",
            base_depth=base_depth + 1,
        )
        if not result.ok or not result.text.strip():
            return None, None  # no synthesis produced; do not name an author for one that is absent
        return result.text, result.target.display_label


def _announce_panel(
    on_progress: ProgressCallback | None,
    included: list[Target],
    skipped: list[SkippedTarget],
) -> None:
    """Report which adapters the auto-expanded panel included and which it skipped (and why)."""
    if on_progress is None:
        return
    names = ", ".join(target.cli for target in included) or "(none)"
    on_progress(f"consensus panel: including {names}")
    for entry in skipped:
        on_progress(f"consensus panel: skipping {entry.cli} ({entry.reason})")


def _stance_for(target: Target, stances: list[Stance] | None, index: int) -> Stance | None:
    """The stance steering a voice: the target's own stance, else the parallel ``stances`` entry."""
    if target.stance is not None:
        return target.stance
    return stances[index] if stances else None


def _panel_voice(voice: DelegationResult) -> PanelVoice:
    """Project a voice's :class:`DelegationResult` into the panel-parent's :class:`PanelVoice` summary."""
    return PanelVoice(
        label=voice.target.display_label,
        ok=voice.ok,
        run_id=Path(voice.run_dir).name if voice.run_dir else None,
        text=voice.text,
        error=voice.error.message if voice.error else None,
        cost=voice.cost,
        changed_files=tuple(voice.changed_files or ()),
        partial=voice.partial,  # a budget-cut voice's harvested stdout, persisted into its artifact (F8a, 2-G)
        session_id=voice.session_id,  # the resume handle, so a cut voice can be continued later (F8a, 2-I)
    )


def _rollup_voice_cost(voices: list[DelegationResult]) -> Cost | None:
    """Sum the answering voices' reported costs into one panel cost for the rollup, or ``None`` if none.

    Each field is summed only over the voices that reported it (a missing field never zeros the total);
    a field no voice reported stays ``None``, so an all-unpriced panel rolls up to ``None`` not a fake zero.
    """
    costs = [voice.cost for voice in voices if voice.cost is not None]
    usd = [cost.usd for cost in costs if cost.usd is not None]
    input_tokens = [cost.input_tokens for cost in costs if cost.input_tokens is not None]
    output_tokens = [cost.output_tokens for cost in costs if cost.output_tokens is not None]
    total_tokens = [cost.total_tokens for cost in costs if cost.total_tokens is not None]
    if not (usd or input_tokens or output_tokens or total_tokens):
        return None
    return Cost(
        usd=sum(usd) if usd else None,
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
        total_tokens=sum(total_tokens) if total_tokens else None,
    )


def _escaped_voice(request: DelegationRequest, exc: BaseException) -> DelegationResult:
    """Fold an exception that escaped one voice's delegation into that voice's structured failure.

    The last line of the panel's "one bad voice never aborts" defense: ``delegate()`` contains the
    known failure surfaces, and this catches whatever still gets out.
    """
    return DelegationResult(
        target=request.target,
        ok=False,
        error=ErrorInfo(code=ErrorCode.INTERNAL, message=f"voice delegation raised: {exc!r}"),
        safety_mode=request.safety_mode,
    )
