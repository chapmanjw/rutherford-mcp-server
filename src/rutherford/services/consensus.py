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

from ..adapters.registry import AdapterRegistry
from ..config.schema import RutherfordConfig
from ..domain.enums import AuthState, SafetyMode, Stance, Strategy
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
from ..runtime.depth import ensure_within_target_cap
from .delegation import DelegationService, ProgressCallback
from .strategies import aggregate, effective_diversity, extract_verdict, verdict_instruction


class ConsensusService:
    """Runs a consensus panel across targets, with optional stances and synthesis."""

    def __init__(
        self,
        delegation: DelegationService,
        config: RutherfordConfig,
        registry: AdapterRegistry,
    ) -> None:
        self._delegation = delegation
        self._config = config
        self._registry = registry

    async def consensus(
        self,
        req: ConsensusRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
    ) -> ConsensusResult | StrategyResult:
        """Fan out ``req`` across its targets and collect every voice.

        With ``expand_all``, the panel is built from every installed + authenticated adapter
        (see :meth:`_expand_all`); otherwise it is the explicit ``targets``. A failing target is
        its own structured voice, so one bad voice never aborts the panel. With a ``strategy`` other
        than ``all-voices``, the voices are aggregated into a :class:`StrategyResult`; otherwise the
        legacy :class:`ConsensusResult` (every voice, plus optional synthesis) is returned unchanged.
        """
        targets, skipped = await self._resolve_targets(req, on_progress)

        async def one(index: int, target_request: DelegationRequest) -> DelegationResult:
            return await self._delegation.delegate(
                target_request,
                correlation_id=f"{correlation_id}:{index}",
                base_depth=base_depth,
                on_progress=on_progress,
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
                include_raw=req.include_raw,
                depth=base_depth,
            )
            for index, target in enumerate(targets)
        ]
        voices = list(await asyncio.gather(*(one(i, r) for i, r in enumerate(requests))))

        if req.strategy is not Strategy.ALL_VOICES:
            return self._aggregate(req, voices, skipped)

        synthesis: str | None = None
        synthesis_by: str | None = None
        if (req.synthesize or self._config.synthesize_default) and voices:
            synthesis, synthesis_by = await self._synthesize(req, voices, correlation_id, base_depth)

        return ConsensusResult(
            voices=voices,
            synthesis=synthesis,
            synthesis_by=synthesis_by,
            skipped=skipped,
            diversity=self._diversity(voices),
        )

    def _diversity(self, voices: list[DelegationResult]) -> DiversityReport | None:
        """Effective model/provider diversity across the voices that answered, or ``None`` if none did."""
        answered = [voice.provenance for voice in voices if voice.ok]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

    def _voice_prompt(self, req: ConsensusRequest, target: Target, index: int) -> str:
        """The prompt for one voice: the question, stance-steered, plus a verdict ask under a strategy."""
        prompt = _apply_stance(req.prompt, _stance_for(target, req.stances, index))
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
        included: list[Target] = []
        skipped: list[SkippedTarget] = []
        for adapter in self._registry.all():
            if adapter.optional:
                skipped.append(SkippedTarget(cli=adapter.id, reason="optional; name it explicitly to include"))
                continue
            detected = await asyncio.to_thread(adapter.detect)
            if not detected.installed:
                skipped.append(SkippedTarget(cli=adapter.id, reason="not installed or not on PATH"))
                continue
            auth = await asyncio.to_thread(adapter.check_auth)
            if auth.state in (AuthState.NEEDS_LOGIN, AuthState.API_KEY_MISSING):
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
            depth=base_depth + 1,
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


def _apply_stance(prompt: str, stance: Stance | None) -> str:
    """Wrap the prompt to steer a voice for or against the proposition."""
    if stance is None or stance is Stance.NEUTRAL:
        return prompt
    if stance is Stance.FOR:
        return f"Argue in favor of the following proposition, making the strongest case for it:\n\n{prompt}"
    return f"Argue against the following proposition, making the strongest case against it:\n\n{prompt}"
