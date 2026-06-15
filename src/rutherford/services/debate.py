# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The debate service: several ACP agents argue a question across rounds, each on a persistent session.

This is the capability the subprocess model could not match. Each voice gets ONE live
:class:`~rutherford.acp.session.ACPSession` held across every round, so round 1 sends the full prompt and
each later round sends only a DELTA (the other voices' latest positions) -- the agent remembers its own
prior reasoning in-session instead of re-reading the whole transcript as fresh input tokens every round.
A voice that fails a round drops out; the sessions are always closed at the end.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession, run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import EFFORT_ORDER, ActivityEventKind, Effort, SafetyMode, Stance, runs_sandboxed
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    ActivityEvent,
    Cost,
    DebateContribution,
    DebateRequest,
    DebateResult,
    DebateRound,
    DelegationResult,
    DiversityReport,
    ErrorInfo,
    PanelInputs,
    PanelTarget,
    RunRollup,
    Target,
    Topology,
)
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_aggregate_cap
from .delegation import ActivityCallback, DelegationService, PanelLifecycle, emit_activity
from .persistence import PanelVoice, write_panel_record
from .strategies import effective_diversity

_log = logging.getLogger("rutherford.services.debate")


@dataclass(frozen=True)
class _Voice:
    """A debate participant: its panel position, resolved target, label, and steering."""

    index: int
    target: Target
    label: str
    stance: Stance | None


class DebateService:
    """Runs a multi-round debate across ACP agents, each on a persistent session, and returns the transcript."""

    def __init__(
        self,
        descriptors: DescriptorRegistry,
        config: RutherfordConfig,
        delegation: DelegationService,
        *,
        ledger: RunLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._descriptors = descriptors
        self._config = config
        #: The delegation service is shared so a debate's turns gate on the SAME concurrency semaphore as
        #: every other path that spawns an agent (a wide debate cannot exceed ``max_concurrency`` live turns).
        self._delegation = delegation
        #: The durable run ledger (F2) for the debate's parent record; ``None`` disables persistence. A debate
        #: drives its turns over persistent :class:`ACPSession`s (not via ``delegate``), so there are no
        #: per-turn child records -- the transcript and the parent record carry the run (decision 1-D).
        self._ledger = ledger
        #: Wall-clock source for parent-record timestamps, injectable so persistence is testable.
        self._clock = clock

    async def debate(
        self,
        req: DebateRequest,
        *,
        base_depth: int = 0,
        on_activity: ActivityCallback | None = None,
    ) -> DebateResult:
        """Open one session per voice, run up to ``rounds`` rounds (delta prompts after round 1), and close.

        Wraps the debate body in a :class:`PanelLifecycle` (N1, item 3) so the activity stream always closes
        with exactly one terminal event. ``on_activity`` is the structured live stream; ``base_depth`` is how
        deep the debate sits in a Rutherford-driving-Rutherford chain, layered onto every voice's session env.
        """
        lifecycle = PanelLifecycle("debate", base_depth, on_activity)
        try:
            return await self._debate_impl(req, lifecycle, base_depth=base_depth, on_activity=on_activity)
        except asyncio.CancelledError:
            lifecycle.on_cancel()
            raise

    async def _debate_impl(
        self,
        req: DebateRequest,
        lifecycle: PanelLifecycle,
        *,
        base_depth: int = 0,
        on_activity: ActivityCallback | None = None,
    ) -> DebateResult:
        """The debate body; the public :meth:`debate` wraps this with the lifecycle guard.

        A ``time_budget_s`` bounds the whole debate's wall-clock, enforced at round boundaries: each round runs
        under the REMAINING budget, a round still in flight at the deadline is cut (its turns finalized as
        ``BUDGET_EXHAUSTED`` contributions, partial preserved but never promoted to a stance), and the
        transcript so far is closed. ``on_budget="continue"`` makes the budget advisory -- every round runs to
        completion. A harvest that leaves fewer than ``min_quorum`` usable positions in the last round is
        ``BUDGET_EXHAUSTED`` (F8a).
        """
        created_at = self._clock()
        persist = self._config.wants_persist(req.persist)
        voices = self._resolve_voices(req)
        rounds_cap = self._resolve_rounds(req)
        # A panel is deliberation, not file work: a debate cannot run a sandboxed (propose / write / yolo)
        # mode. It drives its voices over PERSISTENT sessions held across rounds, directly in the real
        # working_dir -- there is no per-turn worktree to isolate writes into, and no coherent way to merge
        # edits from several arguing agents, so a mutating mode would let an agent write straight into the
        # user's tree. Writes go through delegate (one agent, one worktree sandbox, the reviewed diff applied
        # back). Enforced in the service so the boundary holds no matter which caller set the mode.
        if runs_sandboxed(req.safety_mode):
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"debate runs read-only: it cannot run '{req.safety_mode.value}'. A debate is several agents "
                "arguing a question, not file work; use delegate (a single sandboxed agent) for write / "
                "propose work.",
            )
        cwd = req.working_dir or str(Path.cwd())
        policy = PermissionPolicy(mode=req.safety_mode)
        timeout_s = req.timeout_s or self._config.default_timeout_s
        budget = req.time_budget_s if req.time_budget_s is not None else self._config.default_time_budget_s
        on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
        enforce = budget is not None and on_budget != "continue"

        # N1 (item 3): the declared width (the panel's voices). Cap-check it up front -- refuse with
        # AGENT_CAP_EXCEEDED when enforced, else flag ``over_cap`` -- then announce the panel as started.
        declared = len(voices)
        over_cap = ensure_within_aggregate_cap(
            declared, self._config.max_agents_advisory, enforce=self._config.enforce_agent_cap
        )
        if over_cap:
            _log.warning(
                "debate declared width %d exceeds the aggregate-agent cap %d; watch the activity view",
                declared,
                self._config.max_agents_advisory,
            )
        lifecycle.mark_started(
            ActivityEvent(
                kind=ActivityEventKind.PANEL_STARTED,
                tool="debate",
                depth=base_depth,
                declared=declared,
                message=f"debate panel started: {declared} voice(s)",
            )
        )

        sessions: dict[int, ACPSession] = {}
        open_errors: dict[int, str] = {}
        await self._open_sessions(req, voices, policy, cwd, sessions, open_errors, base_depth)
        start = time.monotonic()
        stop_reason: str | None = None
        try:
            rounds: list[DebateRound] = []
            active = [voice for voice in voices if voice.index in sessions]
            for round_index in range(1, rounds_cap + 1):
                if round_index > 1 and len(active) < 2:
                    break  # a debate needs at least two voices to keep arguing
                remaining: float | None = None
                if enforce and budget is not None:
                    remaining = budget - (time.monotonic() - start)
                    if remaining <= 0:  # the budget is spent at this boundary -- do not start another round
                        stop_reason = "budget"
                        break
                previous = rounds[-1] if rounds else None
                contributions, round_cut = await self._run_round(
                    req,
                    voices,
                    active,
                    sessions,
                    open_errors,
                    round_index,
                    previous,
                    timeout_s,
                    remaining,
                    base_depth,
                    on_activity,
                )
                rounds.append(DebateRound(index=round_index, contributions=contributions))
                if round_cut:  # turns were cut in-flight at the deadline -- finalize over the transcript so far
                    stop_reason = "budget"
                    break
                survivors = {c.seat_id for c in contributions if c.ok and c.text.strip()}
                active = [voice for voice in active if _seat_id(voice) in survivors]

            if stop_reason == "budget":
                self._check_quorum(req, rounds, budget, lifecycle, base_depth, declared)
            final, synthesis_by = await self._synthesize(req, rounds, cwd, timeout_s, base_depth)
            elapsed_s = round(time.monotonic() - start, 3)
            rollup = self._rollup(req, rounds, budget, stop_reason, elapsed_s) if budget is not None else None
            topology = self._topology(declared, rounds, over_cap)
            usable_round = _last_usable_round(rounds)
            usable = sum(1 for c in usable_round.contributions if c.ok and c.text.strip()) if usable_round else 0
            diversity = self._diversity(usable_round)
            run_dir: str | None = None
            if persist and self._ledger is not None:
                run_dir = await asyncio.to_thread(
                    self._write_parent,
                    req,
                    voices,
                    rounds,
                    final,
                    created_at,
                    stop_reason if budget is not None else None,
                    rollup,
                    topology,
                )
            # Surface the F3 effective-lineages headline on the transparency stream (item 5, 5-C).
            finished_message = f"debate panel finished: {len(rounds)} round(s)"
            if diversity is not None:
                finished_message += f" -- {diversity.headline}"
            lifecycle.mark_closed(
                ActivityEvent(
                    kind=ActivityEventKind.PANEL_FINISHED,
                    tool="debate",
                    depth=base_depth,
                    declared=declared,
                    done=usable,
                    observed_agents=topology.observed_peak_agents,
                    message=finished_message,
                )
            )
            return DebateResult(
                prompt=req.prompt,
                rounds=rounds,
                final=final,
                synthesis_by=synthesis_by,
                stop_reason=stop_reason if budget is not None else None,
                rollup=rollup,
                topology=topology,
                diversity=diversity,
                run_dir=run_dir,
            )
        finally:
            await asyncio.gather(*(session.close() for session in sessions.values()), return_exceptions=True)

    def _write_parent(
        self,
        req: DebateRequest,
        voices: list[_Voice],
        rounds: list[DebateRound],
        final: str | None,
        created_at: float,
        stop_reason: str | None,
        rollup: RunRollup | None,
        topology: Topology,
    ) -> str | None:
        """Write the debate's parent record plus the ``transcript.md`` artifact (F2). Best-effort.

        A debate drives its turns over persistent sessions (not via ``delegate``), so there are no per-turn
        child records -- ``transcript.md`` inlines every turn and the parent record carries the run. The
        parent's status derives from the turns (succeeded when any voice ever answered); :class:`PanelInputs`
        captures the resolved roster (each seat + its latest resume handle), the round count, whether a closing
        synthesis ran, and any judge so the debate replays from ``state.json``. Runs off-thread (file I/O).
        """
        assert self._ledger is not None  # guarded by the caller (persist + ledger present)
        contributions = [c for round_ in rounds for c in round_.contributions]
        clis = sorted({c.target.cli for c in contributions})
        # Each seat's latest resume handle across the rounds, recorded in the parent state.json (F8a, 2-I).
        seat_sessions: dict[str, str] = {c.seat_id: c.session_id for c in contributions if c.session_id is not None}
        panel_inputs = PanelInputs(
            targets=[
                PanelTarget(
                    cli=voice.target.cli,
                    model=voice.target.model,
                    stance=voice.stance,
                    session_id=seat_sessions.get(_seat_id(voice)),
                )
                for voice in voices
            ],
            synthesize=req.synthesize,
            rounds=req.rounds,
            judge=req.judge.display_label if req.judge else None,
        )
        return write_panel_record(
            self._ledger,
            run_id=uuid.uuid4().hex,
            kind="debate",
            prompt=req.prompt,
            clis=clis,
            voices=[_panel_voice(c) for c in contributions],
            answer=final or "(no closing synthesis -- see the transcript)",
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
            extra_artifacts={"transcript.md": _render_transcript(req.prompt, rounds)},
        )

    def _diversity(self, usable_round: DebateRound | None) -> DiversityReport | None:
        """Effective model/provider diversity (F3, item 5) across the LAST USABLE round's answering voices.

        A debate's trust signal is whose distinct lineages reached the final exchange, so it is measured over
        the last round that produced answers (the same round the closing summarizes), not every turn ever run.
        ``None`` when no voice survived to a usable round, mirroring the consensus report's "nothing answered,
        nothing to measure" contract.
        """
        if usable_round is None:
            return None
        answered = [c.provenance for c in usable_round.contributions if c.ok and c.text.strip()]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

    def _topology(self, declared: int, rounds: list[DebateRound], over_cap: bool) -> Topology:
        """The debate's observed process/agent fan-out (N1, item 3), summed across every turn of every round.

        ``realized_delegations`` counts each turn's subprocess delegations (a round-1 + round-2 turn for the
        same voice is two delegations -- a debate re-prompts the live session, but each prompt turn is its own
        agent invocation here), so a multi-round debate's realized count exceeds the declared width by design.
        ``observed_peak_agents`` is the max local descendant peak any turn sampled (a FLOOR), ``None`` when no
        turn was sampled. ``over_cap`` flags a declared width over the advisory cap.
        """
        contributions = [c for round_ in rounds for c in round_.contributions]
        observed = [c.observed_peak_agents for c in contributions if c.observed_peak_agents is not None]
        realized = sum(c.delegation_call_count for c in contributions)
        return Topology(
            declared=declared,
            realized_delegations=realized,
            observed_peak_agents=max(observed) if observed else None,
            over_cap=over_cap,
        )

    def _check_quorum(
        self,
        req: DebateRequest,
        rounds: list[DebateRound],
        budget: float | None,
        lifecycle: PanelLifecycle,
        base_depth: int,
        declared: int,
    ) -> None:
        """Raise ``BUDGET_EXHAUSTED`` when a harvest left fewer than ``min_quorum`` usable last-round positions.

        Closes the activity stream with a (failed) ``panel_finished`` before raising, so the stream/push has a
        terminal event for the exhausted-harvest outcome rather than being left open (N1, item 3, decision 3-K).
        """
        usable_round = _last_usable_round(rounds)
        usable = sum(1 for c in usable_round.contributions if c.ok and c.text.strip()) if usable_round else 0
        if usable < self._config.min_quorum:
            budget_s = budget if budget is not None else 0.0
            lifecycle.mark_closed(
                ActivityEvent(
                    kind=ActivityEventKind.PANEL_FINISHED,
                    tool="debate",
                    depth=base_depth,
                    declared=declared,
                    done=usable,
                    status="failed",
                    message=f"debate budget exhausted: {usable} usable position(s), below quorum",
                )
            )
            raise RutherfordError(
                ErrorCode.BUDGET_EXHAUSTED,
                f"time budget ({budget_s:.0f}s) reached with {usable} usable debate position(s), below "
                f"min_quorum ({self._config.min_quorum})",
            )

    async def _open_sessions(
        self,
        req: DebateRequest,
        voices: list[_Voice],
        policy: PermissionPolicy,
        cwd: str,
        sessions: dict[int, ACPSession],
        open_errors: dict[int, str],
        base_depth: int,
    ) -> None:
        """Open one ACP session per voice in parallel; record an unknown-agent or handshake failure.

        Each session carries the debate's producer-effort cap (F8a) and the N1 lineage/depth signal
        (``base_depth``), so every turn on it runs at the resolved tier and a Rutherford-host voice stays
        bounded.
        """

        async def _open(voice: _Voice) -> None:
            if not self._descriptors.has(voice.target.cli):
                open_errors[voice.index] = f"unknown agent id {voice.target.cli!r}"
                return
            session = ACPSession(
                self._descriptors.get(voice.target.cli),
                policy=policy,
                cwd=cwd,
                model=voice.target.model,
                effort=req.effort,
                base_depth=base_depth,
            )
            try:
                await session.open()
            except ACPHandshakeError as exc:
                open_errors[voice.index] = exc.message
                return
            sessions[voice.index] = session

        await asyncio.gather(*(_open(voice) for voice in voices))

    async def _run_round(
        self,
        req: DebateRequest,
        voices: list[_Voice],
        active: list[_Voice],
        sessions: dict[int, ACPSession],
        open_errors: dict[int, str],
        round_index: int,
        previous: DebateRound | None,
        timeout_s: float,
        remaining_budget: float | None,
        base_depth: int,
        on_activity: ActivityCallback | None,
    ) -> tuple[list[DebateContribution], bool]:
        """Run one round in parallel; return ``(contributions, was_cut)``.

        Round 1 also emits a failed contribution for any voice whose session never opened, so the transcript
        shows where a voice fell out. Later rounds run only the surviving active voices. Each turn gates on the
        shared concurrency semaphore and emits its own ``voice_started`` / ``voice_finished`` activity events
        (a correlation id per seat). When ``remaining_budget`` is set the round's turns race under that
        wall-clock deadline: a turn still in flight at the deadline is cut and finalized as a
        ``BUDGET_EXHAUSTED`` contribution (its streamed partial preserved but NOT promoted to text -- a
        rebuttal assumes each voice saw the others' complete positions), and ``was_cut`` is ``True``.
        """

        async def _turn(voice: _Voice) -> DebateContribution:
            prompt = self._round_prompt(req, voice, previous)
            async with self._delegation.semaphore:
                emit_activity(
                    on_activity,
                    ActivityEvent(
                        kind=ActivityEventKind.VOICE_STARTED,
                        correlation_id=_seat_id(voice),  # stable per-seat key across rounds
                        cli=voice.target.cli,
                        model=voice.target.model,
                        role=req.role,
                        depth=base_depth,
                        status="started",
                        message=f"{voice.label} (round {round_index}) started",
                    ),
                )
                result = await sessions[voice.index].prompt(prompt, timeout_s=timeout_s)
            contribution = _to_contribution(voice, round_index, result)
            emit_activity(
                on_activity,
                ActivityEvent(
                    kind=ActivityEventKind.VOICE_FINISHED,
                    correlation_id=_seat_id(voice),
                    cli=voice.target.cli,
                    model=result.target.model,
                    role=req.role,
                    status="ok" if result.ok else "failed",
                    elapsed_s=result.duration_s,
                    observed_agents=result.observed_peak_agents,
                    depth=base_depth,
                    message=f"{voice.label} (round {round_index}) {'ok' if result.ok else 'failed'}",
                ),
            )
            return contribution

        tasks = {voice.index: asyncio.create_task(_turn(voice)) for voice in active}
        cut_indices: set[int] = set()
        if remaining_budget is not None:
            _done, pending = await asyncio.wait(tasks.values(), timeout=max(0.0, remaining_budget))
            if pending:
                emit_activity(
                    on_activity,
                    ActivityEvent(
                        kind=ActivityEventKind.BUDGET_TICK,
                        tool="debate",
                        depth=base_depth,
                        budget_left_s=0.0,
                        message=f"time budget reached; cutting {len(pending)} turn(s) in round {round_index}",
                    ),
                )
                seat_by_index = {voice.index: voice for voice in active}
                for index, task in tasks.items():
                    if task in pending:
                        cut_indices.add(index)
                        task.cancel()
                        seat = seat_by_index[index]
                        emit_activity(
                            on_activity,
                            ActivityEvent(
                                kind=ActivityEventKind.CUT,
                                correlation_id=_seat_id(seat),
                                tool="debate",
                                cli=seat.target.cli,
                                model=seat.target.model,
                                role=req.role,
                                depth=base_depth,
                                status="cut",
                                message=f"{seat.label} cut at the time budget (round {round_index})",
                            ),
                        )
                await asyncio.gather(*pending, return_exceptions=True)
        else:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

        contributions = [
            self._collect_turn(
                req, voice, round_index, tasks[voice.index], sessions[voice.index], voice.index in cut_indices
            )
            for voice in active
        ]
        if round_index == 1:
            for voice in voices:
                if voice.index in open_errors:
                    contributions.append(_failed_contribution(voice, round_index, open_errors[voice.index]))
        contributions.sort(key=lambda contribution: contribution.seat_id)
        return contributions, bool(cut_indices)

    def _collect_turn(
        self,
        req: DebateRequest,
        voice: _Voice,
        round_index: int,
        task: asyncio.Task[DebateContribution],
        session: ACPSession,
        was_cut: bool,
    ) -> DebateContribution:
        """Project one finished-or-cut turn into a contribution; a cut turn is a BUDGET_EXHAUSTED position."""
        if was_cut:
            return _cut_contribution(
                voice, round_index, session, req.time_budget_s or self._config.default_time_budget_s
            )
        if task.cancelled():  # an external cancel we did not induce -- propagate it
            raise asyncio.CancelledError()
        exc = task.exception()
        if exc is not None:
            raise exc
        return task.result()

    def _round_prompt(self, req: DebateRequest, voice: _Voice, previous: DebateRound | None) -> str:
        """Round 1 is the full question; later rounds send only the others' latest positions (a delta).

        The persistent session remembers this voice's own prior answer, so the delta does not re-send it --
        the whole point of holding the session across rounds. A steered voice's FOR/AGAINST stance is
        re-embedded EVERY round, not just round 1 (v2 parity): without the reminder a multi-round debate
        drifts toward the center as each voice accommodates the others, so the assigned side has to be
        restated each round to hold the adversarial framing the stance is for.
        """
        if previous is None:
            return _with_stance(req.prompt, voice.stance)
        others = [
            (contribution.label, contribution.text)
            for contribution in previous.contributions
            if contribution.seat_id != _seat_id(voice) and contribution.ok and contribution.text.strip()
        ]
        block = "\n\n".join(f"## {label}\n{text}" for label, text in others) or "(no other positions)"
        prompt = (
            "This is the next round of our debate. Here are the other participants' latest positions:\n\n"
            f"{block}\n\nCritique them and revise or defend your own answer. End with your current best answer."
        )
        return _with_later_stance(prompt, voice.stance)

    async def _synthesize(
        self, req: DebateRequest, rounds: list[DebateRound], cwd: str, timeout_s: float, base_depth: int
    ) -> tuple[str | None, str | None]:
        """Run a closing pass over the final positions, or ``(None, None)`` when there is nothing to close.

        Uses the caller-named ``judge`` when given (ideally a non-participant), else the first surviving
        voice's agent, on a fresh one-shot session one level deeper (so a Rutherford-host judge is bounded).
        """
        if not req.synthesize or not rounds:
            return None, None
        final_round = _last_usable_round(rounds)
        if final_round is None:
            return None, None
        closing = [c for c in final_round.contributions if c.ok and c.text.strip()]
        if not closing:
            return None, None
        judge = req.judge if req.judge is not None else Target(cli=closing[0].target.cli, model=closing[0].target.model)
        if not self._descriptors.has(judge.cli):
            return None, None
        transcript = "\n\n".join(f"## {c.label}\n{c.text}" for c in closing)
        prompt = (
            "You are closing out a debate among several AI coding agents on the same question.\n\n"
            f"The question:\n{req.prompt}\n\nTheir final positions:\n\n{transcript}\n\n"
            "State where they converged, lay out the remaining disagreements and the strongest case on each "
            "side, and give your best overall answer."
        )
        descriptor = self._descriptors.get(judge.cli)
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
            return None, None
        return result.text, judge.display_label

    def _rollup(
        self,
        req: DebateRequest,
        rounds: list[DebateRound],
        budget: float | None,
        stop_reason: str | None,
        elapsed_s: float,
    ) -> RunRollup:
        """Summarize a time-budgeted debate into its :class:`RunRollup` (F8a).

        ``cut`` counts the literal last round's ``BUDGET_EXHAUSTED`` turns; ``answered`` / ``usable`` are read
        over the last round that produced a usable position (a trailing fully-cut round does not erase the
        positions reached). ``effort_requested`` / ``effort_applied`` are the highest tiers across every turn
        of every round, so the rollup shows what the budget bought.
        """
        last = rounds[-1].contributions if rounds else []
        usable_round = _last_usable_round(rounds)
        usable_contribs = usable_round.contributions if usable_round else []
        cut = sum(1 for c in last if c.error is not None and c.error.code is ErrorCode.BUDGET_EXHAUSTED)
        answered = sum(1 for c in usable_contribs if c.ok)
        usable = sum(1 for c in usable_contribs if c.ok and c.text.strip())
        all_contributions = [c for round_ in rounds for c in round_.contributions]
        applied = [c.effort_applied for c in all_contributions if c.effort_applied is not None]
        effort_applied = max(applied, key=EFFORT_ORDER.index) if applied else None
        requested = [self._resolve_effort(c.target.cli, req.effort) for c in all_contributions]
        present = [tier for tier in requested if tier is not None]
        effort_requested = max(present, key=EFFORT_ORDER.index) if present else None
        return RunRollup(
            stop_reason=stop_reason or "ok",
            requested=len(req.targets),
            answered=answered,
            cut=cut,
            usable=usable,
            quorum_met=usable >= self._config.min_quorum,
            elapsed_s=elapsed_s,
            time_budget_s=budget,
            effort_requested=effort_requested,
            effort_applied=effort_applied,
            cost=_sum_contribution_cost(all_contributions),
        )

    def _resolve_effort(self, cli: str, effort: Effort | None) -> Effort | None:
        """The effort tier a ``cli`` debate seat ran at: the call value, else the configured default (F8a)."""
        return effort if effort is not None else self._config.effort_for(cli)

    def _resolve_voices(self, req: DebateRequest) -> list[_Voice]:
        if len(req.targets) < 2:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT, "a debate needs at least two targets so the voices have someone to argue with"
            )
        if len(req.targets) > self._config.max_targets:
            raise RutherfordError(
                ErrorCode.TOO_MANY_TARGETS,
                f"debate requested {len(req.targets)} targets; the per-call cap is {self._config.max_targets}",
            )
        stances = req.stances if req.stances is not None else []
        labels = _disambiguate([target.display_label for target in req.targets])
        voices: list[_Voice] = []
        for index, target in enumerate(req.targets):
            stance = target.stance if target.stance is not None else (stances[index] if index < len(stances) else None)
            voices.append(_Voice(index=index, target=target, label=labels[index], stance=stance))
        return voices

    def _resolve_rounds(self, req: DebateRequest) -> int:
        if req.rounds < 1:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "rounds must be at least 1")
        if req.rounds > self._config.max_debate_rounds:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"rounds ({req.rounds}) exceeds max_debate_rounds ({self._config.max_debate_rounds})",
            )
        return req.rounds


def _seat_id(voice: _Voice) -> str:
    """A unique seat key, so two voices sharing a ``(cli, model)`` (and label) never merge."""
    return f"{voice.index}:{voice.target.display_label}"


def _disambiguate(labels: list[str]) -> list[str]:
    """Suffix ``#n`` to labels that repeat, so two same-(cli, model) seats are distinguishable."""
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


def _with_stance(prompt: str, stance: Stance | None) -> str:
    if stance is Stance.FOR:
        return f"{prompt}\n\nArgue in favor of the proposition."
    if stance is Stance.AGAINST:
        return f"{prompt}\n\nArgue against the proposition."
    return prompt


def _with_later_stance(prompt: str, stance: Stance | None) -> str:
    """Re-embed a steered voice's stance on a later-round delta prompt (v2 parity: ``Keep arguing ...``)."""
    if stance is Stance.FOR:
        return f"{prompt}\n\nKeep arguing in favor of the proposition."
    if stance is Stance.AGAINST:
        return f"{prompt}\n\nKeep arguing against the proposition."
    return prompt


def _to_contribution(voice: _Voice, round_index: int, result: DelegationResult) -> DebateContribution:
    return DebateContribution(
        label=voice.label,
        seat_id=_seat_id(voice),
        target=result.target,
        round_index=round_index,
        stance=voice.stance,
        ok=result.ok,
        text=result.text,
        duration_s=round(result.duration_s, 3),
        error=result.error,
        session_id=result.session_id,
        provenance=result.provenance,
        cost=result.cost,
        effort_applied=result.effort_applied,
        partial=result.partial,
        # N1 (item 3): carry the turn's observed peak and delegation count up so the debate rolls them into
        # its panel Topology (a floor for observed; one delegation per turn).
        observed_peak_agents=result.observed_peak_agents,
        delegation_call_count=result.delegation_call_count,
        argv=result.argv,  # F2: the turn's resolved launch argv, carried for replay completeness
    )


def _failed_contribution(voice: _Voice, round_index: int, message: str) -> DebateContribution:
    return DebateContribution(
        label=voice.label,
        seat_id=_seat_id(voice),
        target=voice.target,
        round_index=round_index,
        stance=voice.stance,
        ok=False,
        error=ErrorInfo(code=ErrorCode.ACP_HANDSHAKE_FAILED, message=message),
    )


def _cut_contribution(voice: _Voice, round_index: int, session: ACPSession, budget: float | None) -> DebateContribution:
    """A turn cut at the time-budget deadline: a ``BUDGET_EXHAUSTED`` failed position (F8a, 2-F).

    The streamed partial is preserved on the contribution for the transcript/audit but NOT promoted to
    ``text`` -- a rebuttal assumes each voice saw the others' complete positions, so a half-formed stance is a
    trace, not a position. The recovered session id is kept so a later continuation can resume the cut seat.
    """
    partial = session.partial_text.strip() or None
    budget_s = budget if budget is not None else 0.0
    return DebateContribution(
        label=voice.label,
        seat_id=_seat_id(voice),
        target=session.target,
        round_index=round_index,
        stance=voice.stance,
        ok=False,
        error=ErrorInfo(
            code=ErrorCode.BUDGET_EXHAUSTED,
            message=f"{voice.target.cli} was cut at the {budget_s:.0f}s time budget mid-round",
        ),
        session_id=session.session_id,
        partial=partial,
        effort_applied=session.effort_applied,
        # N1 (item 3): a cut turn still spun up a subprocess (count 1) and carries the peak its session
        # sampled before the cut, so the panel topology floor reflects the cut work too.
        observed_peak_agents=session.observed_peak_agents,
        delegation_call_count=1,
    )


def _last_usable_round(rounds: list[DebateRound]) -> DebateRound | None:
    for round_ in reversed(rounds):
        if any(c.ok and c.text.strip() for c in round_.contributions):
            return round_
    return None


def _panel_voice(contribution: DebateContribution) -> PanelVoice:
    """Project one debate turn into the panel-parent's :class:`PanelVoice` summary (status + rollup)."""
    return PanelVoice(
        label=contribution.label,
        ok=contribution.ok,
        run_id=Path(contribution.run_dir).name if contribution.run_dir else None,
        text=contribution.text,
        error=contribution.error.message if contribution.error else None,
        cost=contribution.cost,
        changed_files=tuple(contribution.changed_files),
    )


def _render_transcript(prompt: str, rounds: list[DebateRound]) -> str:
    """Render the full debate as a Markdown ``transcript.md`` artifact for a persisted panel (F2)."""
    lines = [f"# Debate transcript\n\n**Question:** {prompt}\n"]
    for round_ in rounds:
        lines.append(f"\n## Round {round_.index}\n")
        for contribution in round_.contributions:
            status = "" if contribution.ok else " (failed)"
            body = (
                contribution.text.strip()
                if contribution.ok and contribution.text.strip()
                else (contribution.error.message if contribution.error else "(no answer)")
            )
            # A turn cut at the time budget keeps the text it streamed before the cut as a trace (F8a, 2-F).
            if not contribution.ok and contribution.partial and contribution.partial.strip():
                body += f"\n\n#### Partial output (streamed before the cut)\n\n{contribution.partial.strip()}"
            lines.append(f"\n### {contribution.label}{status}\n\n{body}\n")
    return "".join(lines)


def _sum_contribution_cost(contributions: list[DebateContribution]) -> Cost | None:
    """Sum token usage across every turn of every round, or ``None`` when no turn reported any (F8a rollup)."""
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    saw_any = False
    for contribution in contributions:
        if contribution.cost is None:
            continue
        saw_any = True
        for field in totals:
            value = getattr(contribution.cost, field)
            if value is not None:
                totals[field] += value
    if not saw_any:
        return None
    return Cost(**{field: value or None for field, value in totals.items()})
