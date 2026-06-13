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
from ..domain.enums import AuthState, SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    AuthStatus,
    ConsensusRequest,
    ConsensusResult,
    DelegationRequest,
    DelegationResult,
    DetectResult,
    DiversityReport,
    ErrorInfo,
    SkippedTarget,
    StrategyResult,
    Target,
    VoiceVerdict,
)
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_target_cap
from .delegation import DelegationService, ProgressCallback
from .persistence import PanelVoice, render_panel_voices, write_panel_record
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
    ) -> ConsensusResult | StrategyResult:
        """Fan out ``req`` across its targets and collect every voice.

        With ``expand_all``, the panel is built from every installed + authenticated adapter
        (see :meth:`_expand_all`); otherwise it is the explicit ``targets``. A failing target is
        its own structured voice, so one bad voice never aborts the panel. With a ``strategy`` other
        than ``all-voices``, the voices are aggregated into a :class:`StrategyResult`; otherwise the
        legacy :class:`ConsensusResult` (every voice, plus optional synthesis) is returned unchanged.
        """
        created_at = self._clock()
        persist = req.persist if req.persist is not None else (self._config.default_persistence == "job")
        parent_run_id = uuid.uuid4().hex if persist and self._ledger is not None else None
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
                # When the panel persists, each voice is a child record under the parent (F2); when it
                # does not, the voice never self-persists (no orphan per-voice records).
                persist=parent_run_id is not None,
                parent_run_id=parent_run_id,
            )
            for index, target in enumerate(targets)
        ]
        # return_exceptions: an exception that still escapes one voice's delegate() (a buggy adapter
        # surface the containment there missed) must become that voice's structured failure, not
        # abort the gather and discard every healthy sibling -- the panel's headline promise.
        outcomes = await asyncio.gather(*(one(i, r) for i, r in enumerate(requests)), return_exceptions=True)
        voices: list[DelegationResult] = []
        for request, outcome in zip(requests, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):  # a real cancellation still propagates
                raise outcome
            if isinstance(outcome, BaseException):
                voices.append(_escaped_voice(request, outcome))
            else:
                voices.append(outcome)

        result: ConsensusResult | StrategyResult
        answer: str
        synthesized: bool
        if req.strategy is not Strategy.ALL_VOICES:
            result = self._aggregate(req, voices, skipped)
            answer = result.decision or result.outcome
            synthesized = False  # a strategy outcome is a terse decision, not free-text synthesis
        else:
            synthesis: str | None = None
            synthesis_by: str | None = None
            # None means the caller omitted synthesize, the one case the configured default fills;
            # an explicit False wins over a synthesize_default=true config.
            effective_synthesize = req.synthesize if req.synthesize is not None else self._config.synthesize_default
            if effective_synthesize and voices:
                synthesis, synthesis_by = await self._synthesize(req, voices, correlation_id, base_depth)
            result = ConsensusResult(
                voices=voices,
                synthesis=synthesis,
                synthesis_by=synthesis_by,
                skipped=skipped,
                diversity=self._diversity(voices),
            )
            answer = synthesis or "(no synthesis -- see the linked voice records)"
            synthesized = synthesis is not None

        if parent_run_id is not None and self._ledger is not None:
            # Write the parent panel record linking the child voice records (off-thread: file I/O). The
            # parent's status is derived from the voices, and when there is no synthesis a voices.md
            # inlines each voice so the parent is auditable without every child record still on disk.
            panel_voices = [_panel_voice(voice) for voice in voices]
            skipped_pairs = [(entry.cli, entry.reason) for entry in skipped]
            extra = None if synthesized else {"voices.md": render_panel_voices(panel_voices, skipped_pairs)}
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
                extra_artifacts=extra,
            )
        return result

    def _diversity(self, voices: list[DelegationResult]) -> DiversityReport | None:
        """Effective model/provider diversity across the voices that answered, or ``None`` if none did."""
        answered = [voice.provenance for voice in voices if voice.ok]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

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
