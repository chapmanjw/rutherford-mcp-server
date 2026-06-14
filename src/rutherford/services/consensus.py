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
import time
from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession, run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import EFFORT_ORDER, SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    ConsensusRequest,
    ConsensusResult,
    Cost,
    DelegationRequest,
    DelegationResult,
    DiversityReport,
    ErrorInfo,
    Provenance,
    RunRollup,
    SkippedTarget,
    StrategyResult,
    Target,
    VoiceVerdict,
)
from .delegation import DelegationService
from .strategies import aggregate, apply_stance, effective_diversity, extract_verdict, verdict_instruction


class ConsensusService:
    """Runs a consensus panel across ACP agents, with strategies, synthesis, and diversity scoring."""

    def __init__(
        self, delegation: DelegationService, descriptors: DescriptorRegistry, config: RutherfordConfig
    ) -> None:
        self._delegation = delegation
        self._descriptors = descriptors
        self._config = config

    async def consensus(self, req: ConsensusRequest) -> ConsensusResult | StrategyResult:
        """Fan ``req`` out across its targets and reduce the voices.

        With ``expand_all`` (or an empty/``"all"`` target list resolved upstream), the panel is every
        registered agent capped at ``max_targets`` (excluded agents recorded in ``skipped``); otherwise it
        is the explicit, cap-checked ``targets``. A failing target is its own structured voice, so one bad
        voice never aborts the panel. A ``time_budget_s`` caps the whole panel's wall-clock: at the deadline
        the answered voices are kept and the in-flight ones are cut (their partial harvested), then the panel
        aggregates over the harvest as long as ``min_quorum`` usable voices remain -- below that floor it is
        ``BUDGET_EXHAUSTED`` (F8a). With a ``strategy`` other than ``all-voices`` the voices are aggregated
        into a :class:`StrategyResult`; otherwise the :class:`ConsensusResult` (every voice, plus an optional
        synthesis and a diversity report) is returned. Either shape carries ``stop_reason`` + a ``rollup`` when
        a budget governed the run.
        """
        targets, skipped = self._resolve_targets(req)
        budget = req.time_budget_s if req.time_budget_s is not None else self._config.default_time_budget_s
        voices, cut, stop_reason, elapsed_s = await self._fan_out(req, targets, budget)

        if stop_reason == "budget":
            usable = sum(1 for voice in voices if voice.ok and voice.text.strip())
            if usable < self._config.min_quorum:
                raise RutherfordError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    f"time budget ({budget:.0f}s) reached with {usable} usable voice(s), below "
                    f"min_quorum ({self._config.min_quorum})",
                )

        rollup = self._rollup(req, voices, cut, budget, stop_reason, elapsed_s) if budget is not None else None

        if req.strategy is not Strategy.ALL_VOICES:
            result = self._aggregate(req, targets, voices, skipped)
            result.stop_reason = stop_reason
            result.rollup = rollup
            return result

        synthesis, synthesis_by = await self._maybe_synthesize(req, voices)
        return ConsensusResult(
            voices=voices,
            synthesis=synthesis,
            synthesis_by=synthesis_by,
            skipped=skipped,
            diversity=self._diversity(voices),
            stop_reason=stop_reason,
            rollup=rollup,
        )

    async def _fan_out(
        self, req: ConsensusRequest, targets: list[Target], budget: float | None
    ) -> tuple[list[DelegationResult], set[int], str | None, float]:
        """Run every voice, returning ``(voices, cut, stop_reason, elapsed_s)``, budget-aware.

        Without a budget (or with ``on_budget="continue"``, where the budget is advisory and every voice runs
        to completion) this is the plain parallel fan-out and ``stop_reason`` is ``None`` (a clean finish). With
        a binding budget it owns one :class:`~rutherford.acp.session.ACPSession` per voice, races them under an
        :func:`asyncio.wait` deadline, cuts the ones still in flight (harvesting each cut voice's streamed
        partial), and returns ``stop_reason="budget"`` with the cut indices.
        """
        on_budget = req.on_budget if req.on_budget is not None else self._config.default_on_budget
        if budget is None or on_budget == "continue":
            voices = list(await asyncio.gather(*(self._delegate_voice(req, i, t) for i, t in enumerate(targets))))
            return voices, set(), None, 0.0
        return await self._fan_out_budgeted(req, targets, budget)

    async def _delegate_voice(self, req: ConsensusRequest, index: int, target: Target) -> DelegationResult:
        """Run one voice through the delegation primitive (the un-budgeted / continue path)."""
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
        )
        return await self._delegation.delegate(request)

    async def _fan_out_budgeted(
        self, req: ConsensusRequest, targets: list[Target], budget: float
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
            )
            if self._descriptors.has(target.cli)
            else None
            for target in targets
        ]
        start = time.monotonic()
        tasks = [
            asyncio.create_task(self._budget_turn(req, index, target, sessions[index], timeout_s))
            for index, target in enumerate(targets)
        ]
        cut: set[int] = set()
        try:
            _done, pending = await asyncio.wait(tasks, timeout=budget)
            if pending:
                for index, task in enumerate(tasks):
                    if task in pending:
                        cut.add(index)
                        task.cancel()
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
        self, req: ConsensusRequest, index: int, target: Target, session: ACPSession | None, timeout_s: float
    ) -> DelegationResult:
        """Open a voice's session and run its one turn; an unknown agent or handshake failure is a failed voice.

        Held by a task the budget loop may cancel mid-turn; on a cut, the harvested partial is read from the
        live session by :meth:`_collect_voice`, so this method itself never needs to swallow the cancel.
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
            return result
        return await session.prompt(self._voice_prompt(req, target, index), timeout_s=timeout_s)

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
        per agent is a later refinement). Any agent past the ``max_targets`` cap is recorded in
        ``skipped`` with its reason, so the full attempted panel is visible.
        """
        included: list[Target] = []
        skipped: list[SkippedTarget] = []
        for descriptor in self._descriptors.all():
            if len(included) >= self._config.max_targets:
                skipped.append(
                    SkippedTarget(cli=descriptor.id, reason=f"over max_targets ({self._config.max_targets})")
                )
                continue
            included.append(Target(cli=descriptor.id, model=None))
        return included, skipped

    def _voice_prompt(self, req: ConsensusRequest, target: Target, index: int) -> str:
        """The prompt for one voice: the question, stance-steered, plus a verdict ask under a strategy."""
        prompt = apply_stance(req.prompt, _stance_for(target, req.stances, index))
        if req.strategy is not Strategy.ALL_VOICES:
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
        return StrategyResult(
            strategy=req.strategy,
            outcome=outcome,
            decision=decision,
            voices=verdicts,
            skipped=skipped,
            diversity=self._diversity(voices),
        )

    def _diversity(self, voices: list[DelegationResult]) -> DiversityReport | None:
        """Effective model/provider diversity across the voices that answered, or ``None`` if none did."""
        answered = [voice.provenance for voice in voices if voice.ok]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

    async def _maybe_synthesize(
        self, req: ConsensusRequest, voices: list[DelegationResult]
    ) -> tuple[str | None, str | None]:
        """Resolve the tri-state ``synthesize`` and run a combining pass when it is on (``all-voices`` only).

        ``None`` means the caller omitted it -- the one case the configured ``synthesize_default`` fills;
        an explicit ``False`` always wins over a ``synthesize_default=true``.
        """
        effective = req.synthesize if req.synthesize is not None else self._config.synthesize_default
        if not effective or not voices:
            return None, None
        return await self._synthesize(req, voices)

    async def _synthesize(self, req: ConsensusRequest, voices: list[DelegationResult]) -> tuple[str | None, str | None]:
        """Delegate a combining pass to the nominated judge, else the first successful voice.

        Mirrors the debate ``_synthesize`` pattern: a fresh one-shot ACP turn on a read-only session.
        Returns ``(synthesis, synthesizer_label)``, or ``(None, None)`` when no synthesis was produced
        -- no successful voice, an unknown judge, or the synthesis run itself failed -- so ``synthesis_by``
        never names an author for a synthesis that does not exist.
        """
        ok_voices = [voice for voice in voices if voice.ok and voice.text.strip()]
        if not ok_voices:
            return None, None
        first = ok_voices[0].target
        judge = req.judge if req.judge is not None else Target(cli=first.cli, model=first.model)
        if not self._descriptors.has(judge.cli):
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
        cwd = req.working_dir or str(Path.cwd())
        descriptor = self._descriptors.get(judge.cli)
        timeout_s = req.timeout_s or self._config.default_timeout_s
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
