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
import time
from dataclasses import dataclass
from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession, run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import EFFORT_ORDER, Effort, SafetyMode, Stance, is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    Cost,
    DebateContribution,
    DebateRequest,
    DebateResult,
    DebateRound,
    DelegationResult,
    ErrorInfo,
    RunRollup,
    Target,
)


@dataclass(frozen=True)
class _Voice:
    """A debate participant: its panel position, resolved target, label, and steering."""

    index: int
    target: Target
    label: str
    stance: Stance | None


class DebateService:
    """Runs a multi-round debate across ACP agents, each on a persistent session, and returns the transcript."""

    def __init__(self, descriptors: DescriptorRegistry, config: RutherfordConfig) -> None:
        self._descriptors = descriptors
        self._config = config

    async def debate(self, req: DebateRequest) -> DebateResult:
        """Open one session per voice, run up to ``rounds`` rounds (delta prompts after round 1), and close.

        A ``time_budget_s`` bounds the whole debate's wall-clock, enforced at round boundaries: each round runs
        under the REMAINING budget, a round still in flight at the deadline is cut (its turns finalized as
        ``BUDGET_EXHAUSTED`` contributions, partial preserved but never promoted to a stance), and the
        transcript so far is closed. ``on_budget="continue"`` makes the budget advisory -- every round runs to
        completion. A harvest that leaves fewer than ``min_quorum`` usable positions in the last round is
        ``BUDGET_EXHAUSTED`` (F8a).
        """
        voices = self._resolve_voices(req)
        rounds_cap = self._resolve_rounds(req)
        cwd = req.working_dir or str(Path.cwd())
        policy = PermissionPolicy(mode=req.safety_mode)
        if is_mutating(req.safety_mode) and not req.working_dir:
            raise RutherfordError(ErrorCode.WORKSPACE_NOT_TRUSTED, f"{req.safety_mode.value} mode needs a working_dir")
        timeout_s = req.timeout_s or self._config.default_timeout_s
        budget = req.time_budget_s if req.time_budget_s is not None else self._config.default_time_budget_s
        on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
        enforce = budget is not None and on_budget != "continue"

        sessions: dict[int, ACPSession] = {}
        open_errors: dict[int, str] = {}
        await self._open_sessions(req, voices, policy, cwd, sessions, open_errors)
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
                    req, voices, active, sessions, open_errors, round_index, previous, timeout_s, remaining
                )
                rounds.append(DebateRound(index=round_index, contributions=contributions))
                if round_cut:  # turns were cut in-flight at the deadline -- finalize over the transcript so far
                    stop_reason = "budget"
                    break
                survivors = {c.seat_id for c in contributions if c.ok and c.text.strip()}
                active = [voice for voice in active if _seat_id(voice) in survivors]

            if stop_reason == "budget":
                self._check_quorum(req, rounds, budget)
            final, synthesis_by = await self._synthesize(req, rounds, cwd, timeout_s)
            elapsed_s = round(time.monotonic() - start, 3)
            rollup = self._rollup(req, rounds, budget, stop_reason, elapsed_s) if budget is not None else None
            return DebateResult(
                prompt=req.prompt,
                rounds=rounds,
                final=final,
                synthesis_by=synthesis_by,
                stop_reason=stop_reason if budget is not None else None,
                rollup=rollup,
            )
        finally:
            await asyncio.gather(*(session.close() for session in sessions.values()), return_exceptions=True)

    def _check_quorum(self, req: DebateRequest, rounds: list[DebateRound], budget: float | None) -> None:
        """Raise ``BUDGET_EXHAUSTED`` when a harvest left fewer than ``min_quorum`` usable last-round positions."""
        usable_round = _last_usable_round(rounds)
        usable = sum(1 for c in usable_round.contributions if c.ok and c.text.strip()) if usable_round else 0
        if usable < self._config.min_quorum:
            budget_s = budget if budget is not None else 0.0
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
    ) -> None:
        """Open one ACP session per voice in parallel; record an unknown-agent or handshake failure.

        Each session carries the debate's producer-effort cap (F8a), so every turn on it runs at the resolved
        tier and reports ``effort_applied``.
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
    ) -> tuple[list[DebateContribution], bool]:
        """Run one round in parallel; return ``(contributions, was_cut)``.

        Round 1 also emits a failed contribution for any voice whose session never opened, so the transcript
        shows where a voice fell out. Later rounds run only the surviving active voices. When
        ``remaining_budget`` is set the round's turns race under that wall-clock deadline: a turn still in
        flight at the deadline is cut and finalized as a ``BUDGET_EXHAUSTED`` contribution (its streamed
        partial preserved but NOT promoted to text -- a rebuttal assumes each voice saw the others' complete
        positions), and ``was_cut`` is ``True``.
        """

        async def _turn(voice: _Voice) -> DebateContribution:
            prompt = self._round_prompt(req, voice, previous)
            result = await sessions[voice.index].prompt(prompt, timeout_s=timeout_s)
            return _to_contribution(voice, round_index, result)

        tasks = {voice.index: asyncio.create_task(_turn(voice)) for voice in active}
        cut_indices: set[int] = set()
        if remaining_budget is not None:
            _done, pending = await asyncio.wait(tasks.values(), timeout=max(0.0, remaining_budget))
            if pending:
                for index, task in tasks.items():
                    if task in pending:
                        cut_indices.add(index)
                        task.cancel()
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
        the whole point of holding the session across rounds.
        """
        if previous is None:
            return _with_stance(req.prompt, voice.stance)
        others = [
            (contribution.label, contribution.text)
            for contribution in previous.contributions
            if contribution.seat_id != _seat_id(voice) and contribution.ok and contribution.text.strip()
        ]
        block = "\n\n".join(f"## {label}\n{text}" for label, text in others) or "(no other positions)"
        return (
            "This is the next round of our debate. Here are the other participants' latest positions:\n\n"
            f"{block}\n\nCritique them and revise or defend your own answer. End with your current best answer."
        )

    async def _synthesize(
        self, req: DebateRequest, rounds: list[DebateRound], cwd: str, timeout_s: float
    ) -> tuple[str | None, str | None]:
        """Run a closing pass over the final positions, or ``(None, None)`` when there is nothing to close.

        Uses the caller-named ``judge`` when given (ideally a non-participant), else the first surviving
        voice's agent, on a fresh one-shot session.
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
    )


def _last_usable_round(rounds: list[DebateRound]) -> DebateRound | None:
    for round_ in reversed(rounds):
        if any(c.ok and c.text.strip() for c in round_.contributions):
            return round_
    return None


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
