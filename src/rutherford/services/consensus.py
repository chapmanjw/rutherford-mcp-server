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
from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    ConsensusRequest,
    ConsensusResult,
    DelegationRequest,
    DelegationResult,
    DiversityReport,
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
        voice never aborts the panel. With a ``strategy`` other than ``all-voices`` the voices are
        aggregated into a :class:`StrategyResult`; otherwise the :class:`ConsensusResult` (every voice,
        plus an optional synthesis and a diversity report) is returned.
        """
        targets, skipped = self._resolve_targets(req)

        async def _one(index: int, target: Target) -> DelegationResult:
            request = DelegationRequest(
                target=target,
                prompt=self._voice_prompt(req, target, index),
                working_dir=req.working_dir,
                files=req.files,
                # A per-seat ``Target.role`` overrides the call-level role for just this voice.
                role=target.role or req.role,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
            )
            return await self._delegation.delegate(request)

        voices = list(await asyncio.gather(*(_one(index, target) for index, target in enumerate(targets))))

        if req.strategy is not Strategy.ALL_VOICES:
            return self._aggregate(req, targets, voices, skipped)

        synthesis, synthesis_by = await self._maybe_synthesize(req, voices)
        return ConsensusResult(
            voices=voices,
            synthesis=synthesis,
            synthesis_by=synthesis_by,
            skipped=skipped,
            diversity=self._diversity(voices),
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


def _stance_for(target: Target, stances: list[Stance] | None, index: int) -> Stance | None:
    """The stance steering a voice: the target's own stance, else the parallel ``stances`` entry."""
    if target.stance is not None:
        return target.stance
    return stances[index] if stances else None
