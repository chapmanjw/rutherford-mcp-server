# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The debate service: several targets argue a question across multiple rounds.

Where :class:`~rutherford.services.consensus.ConsensusService` asks each voice once in isolation,
a debate runs in rounds. Round one collects every voice's independent answer; each later round
shows a voice the other voices' latest positions and asks it to critique and revise its own. Every
turn is recorded as a :class:`~rutherford.domain.models.DebateContribution`, so the returned
:class:`~rutherford.domain.models.DebateResult` is a full transcript a reader can retrace -- the
"thinking out loud" that a terse consensus result drops. One failing voice is recorded as a failed
turn and falls out of later rounds; it never aborts the debate. The debate spawns up to
``voices x rounds`` subprocesses, so the per-call target cap and the configured round cap bound it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config.schema import RutherfordConfig
from ..domain.enums import EFFORT_ORDER, SafetyMode, Stance
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    Cost,
    DebateContribution,
    DebateRequest,
    DebateResult,
    DebateRound,
    DelegationRequest,
    DelegationResult,
    DiversityReport,
    ErrorInfo,
    PanelInputs,
    PanelTarget,
    RunRollup,
    Target,
)
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_target_cap
from .delegation import DelegationService, ProgressCallback
from .persistence import PanelVoice, live_tee, stop_live_tee, write_panel_record
from .strategies import apply_stance, effective_diversity


@dataclass(frozen=True)
class _Voice:
    """A debate participant: its panel position, resolved target, and steering."""

    index: int
    target: Target
    label: str
    #: A unique key for survival and own-position lookup, so two seats sharing a ``(cli, model)`` --
    #: and therefore a display ``label`` -- do not collapse into one survivor.
    seat_id: str
    stance: Stance | None
    role: str | None


class DebateService:
    """Runs a multi-round debate across targets and returns the full transcript."""

    def __init__(
        self,
        delegation: DelegationService,
        config: RutherfordConfig,
        *,
        ledger: RunLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._delegation = delegation
        self._config = config
        #: The durable run ledger (F2) for the debate's parent record; ``None`` disables persistence.
        self._ledger = ledger
        self._clock = clock

    async def debate(
        self,
        req: DebateRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
    ) -> DebateResult:
        """Run ``req`` across its targets for up to ``rounds`` rounds and return the transcript.

        Round one is independent; later rounds let each voice rebut the others. A voice that fails a
        round is recorded and drops out, and the debate stops early once fewer than two voices remain.
        With ``synthesize``, a closing pass over the final positions states where the panel landed.
        """
        voices = self._resolve_voices(req)
        rounds_cap = self._resolve_rounds(req)
        created_at = self._clock()
        persist = self._config.wants_persist(req.persist)
        parent_run_id = uuid.uuid4().hex if persist and self._ledger is not None else None

        # Time-budget harvest (F8a, 2-A'/2-where/2-behavior): each round runs under the REMAINING wall-clock
        # budget via ``asyncio.wait``; a turn still in flight when the deadline is reached is cut (its
        # process tree killed) and the debate finalizes over the transcript so far. ``continue`` makes the
        # budget advisory (run every round to completion); ``harvest`` (default) and ``resume`` both cut at
        # the deadline (``resume``'s deliberate come-back rides the item-9 continuation primitive -- OnBudget).
        budget = req.time_budget_s if req.time_budget_s is not None else self._config.default_time_budget_s
        # The disposition: the call value, else the configured default (2-M's per-call + workspace default).
        on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
        enforce = budget is not None and on_budget != "continue"
        stop_reason: str | None = None

        rounds: list[DebateRound] = []
        # The voices still in the debate; a failed turn removes its voice from later rounds.
        active = list(voices)
        for round_index in range(1, rounds_cap + 1):
            if round_index > 1 and len(active) < 2:
                break  # a debate needs at least two voices to rebut one another
            remaining: float | None = None
            if enforce and budget is not None:
                remaining = budget - (self._clock() - created_at)
                if remaining <= 0:  # the budget is already spent at this boundary -- do not start a round
                    stop_reason = "budget"
                    _announce(on_progress, f"debate: time budget ({budget:.0f}s) reached before round {round_index}")
                    break
            _announce(on_progress, f"debate: round {round_index} of {rounds_cap} ({len(active)} voices)")
            previous = rounds[-1] if rounds else None
            contributions, round_cut = await self._run_round(
                req, active, round_index, previous, correlation_id, base_depth, on_progress, parent_run_id, remaining
            )
            if round_cut:  # turns were cut in-flight at the deadline -- finalize over the transcript so far
                stop_reason = "budget"
                # Keep the cut round even when no turn finished: its cut turns carry recovered resume sessions
                # (2-I) and streamed traces (2-F) that must survive into the result/record. The closing and
                # the quorum gate below run over the last round with USABLE positions, so a trailing fully-cut
                # round preserves its handles without being mistaken for the panel's outcome (2-behavior).
                rounds.append(DebateRound(index=round_index, contributions=contributions))
                _announce(
                    on_progress, f"debate: round {round_index} cut at the time budget ({budget:.0f}s); finalizing"
                )
                break
            rounds.append(DebateRound(index=round_index, contributions=contributions))
            survivors = {c.seat_id for c in contributions if c.ok}
            active = [voice for voice in active if voice.seat_id in survivors]

        # 2-E': a budget harvest that left fewer than min_quorum usable positions (in the last round that has
        # any) is a genuine failure (BUDGET_EXHAUSTED), raised before any result/persist; otherwise the
        # harvest is a success. The "last usable round" skips a trailing fully-cut round (kept for its handles).
        if stop_reason == "budget":
            usable_round = _last_usable_round(rounds)
            usable = sum(1 for c in usable_round.contributions if c.ok and c.text.strip()) if usable_round else 0
            if usable < self._config.min_quorum:
                raise RutherfordError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"time budget ({budget:.0f}s) reached with {usable} usable voice(s), below "
                    f"min_quorum ({self._config.min_quorum})",
                )

        final, synthesis_by = await self._synthesize_final(req, rounds, correlation_id, base_depth, on_progress)
        rollup = (
            self._rollup(req, rounds, budget, stop_reason, self._clock() - created_at) if budget is not None else None
        )
        result = DebateResult(
            prompt=req.prompt,
            rounds=rounds,
            final=final,
            synthesis_by=synthesis_by,
            diversity=self._diversity(rounds),
            stop_reason=stop_reason,
            rollup=rollup,
        )
        if parent_run_id is not None and self._ledger is not None:
            # Write the parent panel record linking every turn's child record, plus the transcript. The
            # parent's status is derived from the turns (succeeded when any voice ever answered); the
            # transcript.md already inlines every turn, so no separate voices.md is needed here.
            contributions = [c for round_ in rounds for c in round_.contributions]
            clis = sorted({c.target.cli for c in contributions})
            # Each seat's latest resume handle across the rounds, so the parent roster records it in
            # state.toon for a later continuation (F8a, 2-I), matching the consensus parent.
            seat_sessions: dict[str, str] = {c.seat_id: c.session_id for c in contributions if c.session_id is not None}
            panel_inputs = PanelInputs(
                targets=[
                    PanelTarget(
                        cli=v.target.cli, model=v.target.model, stance=v.stance, session_id=seat_sessions.get(v.seat_id)
                    )
                    for v in voices
                ],
                synthesize=req.synthesize,
                rounds=req.rounds,
                judge=req.judge.display_label if req.judge else None,
            )
            result.run_dir = await asyncio.to_thread(
                write_panel_record,
                self._ledger,
                run_id=parent_run_id,
                kind="debate",
                prompt=req.prompt,
                clis=clis,
                voices=[_panel_voice(c) for c in contributions],
                answer=final or "(no closing synthesis -- see the linked voice records)",
                created_at=created_at,
                finished_at=self._clock(),
                safety_mode=req.safety_mode,
                cwd=req.working_dir,
                files=req.files,
                role=req.role,
                panel=panel_inputs,
                stop_reason=stop_reason,
                rollup=rollup,
                extra_artifacts={"transcript.md": _render_transcript(req.prompt, rounds)},
            )
        return result

    def _diversity(self, rounds: list[DebateRound]) -> DiversityReport | None:
        """Effective diversity across the last usable round's answering voices, or ``None`` if none.

        Uses the last round that has usable positions (skipping a trailing fully-cut budget round), so the
        diversity reflects the positions the closing actually summarized.
        """
        usable_round = _last_usable_round(rounds)
        if usable_round is None:
            return None
        answered = [c.provenance for c in usable_round.contributions if c.ok]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

    def _rollup(
        self,
        req: DebateRequest,
        rounds: list[DebateRound],
        budget: float | None,
        stop_reason: str | None,
        elapsed_s: float,
    ) -> RunRollup:
        """Summarize a budget-governed debate: final-round counts, quorum, highest effort, summed cost.

        Reported over the LAST round (the set the closing runs over). ``cut`` is the turns of that round
        cancelled at the time-budget deadline (a ``BUDGET_EXHAUSTED`` contribution); ``answered`` is the
        turns that finished; the rounds-run detail lives on :attr:`DebateResult.rounds` (one entry per
        round). ``cost`` and the highest applied effort are summed across every turn in every round.
        """
        # ``cut`` is the turns cancelled in the LITERAL last round (the one the deadline hit); ``answered`` /
        # ``usable`` are over the last round with usable positions (the harvested set the closing ran over),
        # which skips a trailing fully-cut round -- so a fully-cut final round does not zero the usable count.
        last = rounds[-1].contributions if rounds else []
        usable_round = _last_usable_round(rounds)
        usable_contribs = usable_round.contributions if usable_round else []
        cut = sum(1 for c in last if c.error is not None and c.error.code is ErrorCode.BUDGET_EXHAUSTED)
        answered = sum(1 for c in usable_contribs if c.ok)
        usable = sum(1 for c in usable_contribs if c.ok and c.text.strip())
        all_contributions = [c for round_ in rounds for c in round_.contributions]
        applied = [c.effort_applied for c in all_contributions if c.effort_applied is not None]
        effort_applied = max(applied, key=EFFORT_ORDER.index) if applied else None
        # The RESOLVED requested effort, derived per seat so a per-adapter ``[adapters.<id>].effort`` default
        # is reflected (not just the global default): the highest tier any turn actually requested.
        requested = [self._delegation.resolve_effort(c.target.cli, req.effort) for c in all_contributions]
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
            cost=_rollup_contribution_cost(all_contributions),
        )

    def _resolve_voices(self, req: DebateRequest) -> list[_Voice]:
        """Validate the panel and build the ordered list of debate voices."""
        if len(req.targets) < 2:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                "a debate needs at least two targets so the voices have someone to argue with",
            )
        ensure_within_target_cap(len(req.targets), self._config.max_targets)
        if req.stances is not None and len(req.stances) != len(req.targets):
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"stances ({len(req.stances)}) must match targets ({len(req.targets)})",
            )
        # Disambiguate duplicate display labels (two unlabeled same-(cli, model) seats) so the
        # transcript is unambiguous, while seat_id (index-based) keeps survival/lookup unique. A
        # generated "#N" suffix skips any label already in use -- an explicit label or one already
        # assigned -- so a caller who hand-labels a seat "claude_code#2" cannot collide with the
        # auto-generated suffix for an unlabeled claude_code seat.
        base_labels = [target.display_label for target in req.targets]
        duplicated = {label for label in base_labels if base_labels.count(label) > 1}
        taken: set[str] = set(base_labels)
        seen: dict[str, int] = {}
        voices: list[_Voice] = []
        for index, target in enumerate(req.targets):
            base = target.display_label
            if base in duplicated:
                seen[base] = seen.get(base, 0) + 1
                label = base if seen[base] == 1 else _next_free_label(base, taken)
            else:
                label = base
            taken.add(label)
            voices.append(
                _Voice(
                    index=index,
                    target=target,
                    label=label,
                    seat_id=f"{index}:{base}",
                    stance=target.stance
                    if target.stance is not None
                    else (req.stances[index] if req.stances else None),
                    role=target.role or req.role,
                )
            )
        return voices

    def _resolve_rounds(self, req: DebateRequest) -> int:
        """Validate the requested round count against the configured cap."""
        if req.rounds < 1:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "rounds must be at least 1")
        if req.rounds > self._config.max_debate_rounds:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"rounds ({req.rounds}) exceeds max_debate_rounds ({self._config.max_debate_rounds})",
            )
        return req.rounds

    async def _run_round(
        self,
        req: DebateRequest,
        voices: list[_Voice],
        round_index: int,
        previous: DebateRound | None,
        correlation_id: str,
        base_depth: int,
        on_progress: ProgressCallback | None,
        parent_run_id: str | None,
        remaining_budget: float | None,
    ) -> tuple[list[DebateContribution], bool]:
        """Run one round: every active voice answers (round 1) or rebuts (later rounds) in parallel.

        Returns the contributions plus whether the round was CUT at the time-budget deadline. With
        ``remaining_budget`` set, the round's turns run as tasks under an ``asyncio.wait`` deadline (F8a,
        2-where); a turn still in flight at the deadline is cancelled (the runner kills its process tree)
        and recorded as a ``BUDGET_EXHAUSTED`` contribution, while the turns that finished keep their
        answers -- so the closing runs over the transcript so far (2-behavior). ``None`` runs every turn to
        completion (no budget, or ``on_budget`` continue). One seat's escaped exception becomes that seat's
        failed contribution, never a round abort; an external cancel (e.g. job_cancel) propagates after the
        tasks are cancelled and drained, so no subprocess tree is leaked.
        """

        # Per-turn stdout accumulators (F8a, 2-F: capture always): a turn cut at the deadline preserves the
        # stdout it streamed before the cut as a trace on its contribution, even though a cut debate turn is
        # never promoted to a stance.
        partials: list[list[str]] = [[] for _ in voices]

        async def one(index: int, voice: _Voice) -> DebateContribution:
            prompt = self._round_prompt(req, voice, previous)
            request = DelegationRequest(
                target=voice.target,
                prompt=prompt,
                working_dir=req.working_dir,
                files=req.files,
                role=voice.role,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
                effort=req.effort,  # the debate's producer-effort cap flows to every turn (F8a)
                include_raw=req.include_raw,
                # When the debate persists, each turn is a child record under the parent (F2).
                persist=parent_run_id is not None,
                parent_run_id=parent_run_id,
            )
            result = await self._delegation.delegate(
                request,
                correlation_id=f"{correlation_id}:r{round_index}:{voice.index}",
                base_depth=base_depth,
                on_progress=on_progress,
                on_stdout=partials[index].append,
            )
            return _to_contribution(voice, round_index, result)

        tasks = [asyncio.create_task(one(index, voice)) for index, voice in enumerate(voices)]
        cut: set[int] = set()
        # Stream-to-job (F8a, 2-G): while a persisted round runs, tee each turn's accumulating stdout into the
        # job artifacts off-thread (namespaced per round), so a cut turn's in-flight work survives a crash.
        tee = parent_run_id if (parent_run_id is not None and self._ledger is not None) else None
        tee_stop = asyncio.Event()
        tee_task = (
            asyncio.create_task(live_tee(self._ledger, tee, f"round-{round_index}-voice", partials, tee_stop))
            if tee and self._ledger is not None
            else None
        )
        try:
            if remaining_budget is not None:
                _done, pending = await asyncio.wait(tasks, timeout=max(0.0, remaining_budget))
                if pending:
                    for index, task in enumerate(tasks):
                        if task in pending:
                            cut.add(index)
                            task.cancel()
                    # MANDATORY cancel-then-drain: the runner kills the CLI process tree only once the
                    # cancellation is delivered AND awaited -- skipping this leaks orphaned subprocesses.
                    await asyncio.gather(*pending, return_exceptions=True)
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # An external cancel of the whole debate: asyncio.wait/gather do not cancel the turn tasks for
            # us, so cancel + drain them (no orphaned trees) before propagating.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            await stop_live_tee(tee_task, tee_stop)  # final snapshot of the round's live stream, then exit

        contributions: list[DebateContribution] = []
        for index, (voice, task) in enumerate(zip(voices, tasks, strict=True)):
            if task.cancelled():
                if index in cut:
                    contributions.append(self._cut_contribution(voice, round_index, req, partials[index]))
                    continue
                raise asyncio.CancelledError()  # an external cancel we did not induce -- propagate it
            exc = task.exception()
            if isinstance(exc, asyncio.CancelledError):
                raise exc
            if exc is not None:
                failed = DelegationResult(
                    target=voice.target,
                    ok=False,
                    error=ErrorInfo(code=ErrorCode.INTERNAL, message=f"voice delegation raised: {exc!r}"),
                    safety_mode=req.safety_mode,
                )
                contributions.append(_to_contribution(voice, round_index, failed))
            else:
                contributions.append(task.result())
        return contributions, bool(cut)

    def _cut_contribution(
        self, voice: _Voice, round_index: int, req: DebateRequest, partial_lines: list[str]
    ) -> DebateContribution:
        """A turn cut at the debate's time-budget deadline: a ``BUDGET_EXHAUSTED`` failed contribution.

        A debate turn cut mid-flight is recorded as a failed contribution (not promoted to a partial answer,
        unlike a consensus voice): a rebuttal round assumes each voice saw the others' complete positions, so
        a half-streamed turn is a trace, not a position the closing should treat as a stance. The stdout it
        streamed before the cut is still preserved on ``partial`` (F8a, 2-F: capture always), for the
        transcript and audit, rather than discarded. The turn ran with the resolved effort, so it is reported
        (2-L-map); and any resumable session the partial established is recovered (2-I) so the parent roster
        can record this seat's handle for a later continuation, even though the turn produced no answer.
        """
        effort = self._delegation.resolve_effort(voice.target.cli, req.effort)
        partial = "\n".join(partial_lines).strip() or None
        cut = DelegationResult(
            target=voice.target,
            ok=False,
            error=ErrorInfo(code=ErrorCode.BUDGET_EXHAUSTED, message="cut at the debate time-budget deadline"),
            safety_mode=req.safety_mode,
            stop_reason="budget",
            partial=partial,
            session_id=self._delegation.recover_session(voice.target, partial or "", req.safety_mode, effort),
            effort=effort,
            effort_applied=self._delegation.applied_effort(voice.target.cli, effort),
        )
        return _to_contribution(voice, round_index, cut)

    def _round_prompt(self, req: DebateRequest, voice: _Voice, previous: DebateRound | None) -> str:
        """Build the prompt for ``voice`` this round: a fresh answer, or a rebuttal of the others."""
        if previous is None:
            return apply_stance(req.prompt, voice.stance)
        own = _latest_text(previous, voice.seat_id)
        others = [
            (contribution.label, contribution.text)
            for contribution in previous.contributions
            if contribution.seat_id != voice.seat_id and contribution.ok and contribution.text.strip()
        ]
        return _rebuttal_prompt(req.prompt, own, others, voice.stance)

    async def _synthesize_final(
        self,
        req: DebateRequest,
        rounds: list[DebateRound],
        correlation_id: str,
        base_depth: int,
        on_progress: ProgressCallback | None,
    ) -> tuple[str | None, str | None]:
        """Delegate a closing pass over the final positions, stating where the panel landed.

        Returns ``(final, synthesizer_label)``, or ``(None, None)`` when no synthesis was produced --
        no surviving voice, or the synthesis run itself failed -- so ``synthesis_by`` never names an
        author for a synthesis that does not exist. Uses the caller-named ``judge`` when given
        (ideally a non-participant), otherwise the first surviving voice, whose disambiguated debate
        label is reported so the reader can map it back to a transcript seat.
        """
        if not req.synthesize or not rounds:
            return None, None
        final_round = _last_usable_round(rounds)  # skip a trailing fully-cut budget round (no positions)
        if final_round is None:
            return None, None
        closing = [c for c in final_round.contributions if c.ok and c.text.strip()]
        if not closing:
            return None, None
        _announce(on_progress, "debate: synthesizing the closing statement")
        transcript = "\n\n".join(f"## {c.label}\n{c.text}" for c in closing)
        prompt = (
            "You are closing out a debate among several AI coding agents on the same question.\n\n"
            f"The question:\n{req.prompt}\n\n"
            f"Their final positions:\n\n{transcript}\n\n"
            "Write the closing summary: state where they converged, lay out the remaining "
            "disagreements and the strongest case on each side, and give your best overall answer."
        )
        judge_target = req.judge or closing[0].target
        synth_request = DelegationRequest(
            target=judge_target,
            prompt=prompt,
            working_dir=req.working_dir,
            safety_mode=SafetyMode.READ_ONLY,
            timeout_s=req.timeout_s,
            persist=False,  # the closing synthesis is internal; not its own job record (F2)
        )
        result = await self._delegation.delegate(
            synth_request,
            correlation_id=f"{correlation_id}:final",
            base_depth=base_depth + 1,
        )
        if not result.ok or not result.text.strip():
            return None, None  # no synthesis produced; do not name an author for one that is absent
        # For an explicit judge, report the target that actually answered (reflects any model
        # fallback); for the default first-survivor path, report that seat's disambiguated debate
        # label so the reader can map synthesis_by back to a transcript seat.
        synthesizer_label = result.target.display_label if req.judge else closing[0].label
        return result.text, synthesizer_label


def _last_usable_round(rounds: list[DebateRound]) -> DebateRound | None:
    """The last round that holds at least one usable position (an ``ok`` turn with text), or ``None``.

    A budget cut can leave a trailing round with no completed turns (kept for its recovered resume handles
    and traces, F8a 2-I/2-F); this skips past it to the round whose positions the closing should summarize
    and the quorum gate should weigh.
    """
    for round_ in reversed(rounds):
        if any(c.ok and c.text.strip() for c in round_.contributions):
            return round_
    return None


def _next_free_label(base: str, taken: set[str]) -> str:
    """Return the first ``base#n`` (n >= 2) not already in ``taken``.

    Used to disambiguate duplicate seat labels without ever colliding with a label that already
    exists -- an explicit caller-supplied one or a previously assigned generated one.
    """
    n = 2
    while f"{base}#{n}" in taken:
        n += 1
    return f"{base}#{n}"


def _latest_text(round_: DebateRound, seat_id: str) -> str:
    """Return this seat's answer text from a round, or empty if it did not contribute."""
    for contribution in round_.contributions:
        if contribution.seat_id == seat_id:
            return contribution.text
    return ""


def _to_contribution(voice: _Voice, round_index: int, result: DelegationResult) -> DebateContribution:
    """Fold a delegation result into a transcript contribution for ``voice``."""
    return DebateContribution(
        label=voice.label,
        seat_id=voice.seat_id,
        target=result.target,
        round_index=round_index,
        stance=voice.stance,
        role=voice.role,
        ok=result.ok,
        text=result.text,
        raw=result.raw,
        duration_s=result.duration_s,
        error=result.error,
        fallback_from=result.fallback_from,
        session_id=result.session_id,  # the resume handle, carried so the parent roster can record it (2-I)
        partial=result.partial,  # stdout streamed before a time-budget cut, preserved as a trace (F8a, 2-F)
        provenance=result.provenance,
        cost=result.cost,
        effort_applied=result.effort_applied,
        changed_files=list(result.changed_files or []),
        run_dir=result.run_dir,
    )


def _rollup_contribution_cost(contributions: list[DebateContribution]) -> Cost | None:
    """Sum the turns' reported costs into one debate cost for the rollup, or ``None`` when none reported.

    Each field is summed only over the turns that reported it (a missing field never zeros the total); a
    field no turn reported stays ``None``, so an all-unpriced debate rolls up to ``None`` not a fake zero.
    """
    costs = [c.cost for c in contributions if c.cost is not None]
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


def _panel_voice(contribution: DebateContribution) -> PanelVoice:
    """Project one debate turn into the panel-parent's :class:`PanelVoice` summary (status + child link)."""
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
            # A turn cut at the time budget keeps the stdout it streamed before the cut as a trace (F8a, 2-F).
            if not contribution.ok and contribution.partial and contribution.partial.strip():
                body += f"\n\n#### Partial output (streamed before the cut)\n\n{contribution.partial.strip()}"
            lines.append(f"\n### {contribution.label}{status}\n\n{body}\n")
    return "".join(lines)


def _rebuttal_prompt(
    question: str,
    own: str,
    others: list[tuple[str, str]],
    stance: Stance | None,
) -> str:
    """Build a later-round prompt: show a voice its own and the others' positions and ask it to revise."""
    others_block = "\n\n".join(f"## {label}\n{text}" for label, text in others) or "(no other positions)"
    parts = [
        "You are in a multi-round debate among several AI coding agents on this question:",
        question,
        "Your previous position:",
        own or "(you did not answer yet)",
        "The other participants' latest positions:",
        others_block,
        "Critique the other positions and revise or defend your own. Be specific about where you "
        "agree, where you disagree and why, and what (if anything) changes your mind. End with your "
        "current best answer.",
    ]
    if stance is Stance.FOR:
        parts.append("Keep arguing in favor of the proposition.")
    elif stance is Stance.AGAINST:
        parts.append("Keep arguing against the proposition.")
    return "\n\n".join(parts)


def _announce(on_progress: ProgressCallback | None, message: str) -> None:
    """Emit a progress line if a callback is listening (surfaced for async jobs via job_status)."""
    if on_progress is not None:
        on_progress(message)
