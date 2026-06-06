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
from dataclasses import dataclass

from ..config.schema import RutherfordConfig
from ..domain.enums import SafetyMode, Stance
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    DebateContribution,
    DebateRequest,
    DebateResult,
    DebateRound,
    DelegationRequest,
    DelegationResult,
    Target,
)
from ..runtime.depth import ensure_within_target_cap
from .delegation import DelegationService, ProgressCallback


@dataclass(frozen=True)
class _Voice:
    """A debate participant: its panel position, resolved target, and steering."""

    index: int
    target: Target
    label: str
    stance: Stance | None


class DebateService:
    """Runs a multi-round debate across targets and returns the full transcript."""

    def __init__(self, delegation: DelegationService, config: RutherfordConfig) -> None:
        self._delegation = delegation
        self._config = config

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

        rounds: list[DebateRound] = []
        # The voices still in the debate; a failed turn removes its voice from later rounds.
        active = list(voices)
        for round_index in range(1, rounds_cap + 1):
            if round_index > 1 and len(active) < 2:
                break  # a debate needs at least two voices to rebut one another
            _announce(on_progress, f"debate: round {round_index} of {rounds_cap} ({len(active)} voices)")
            previous = rounds[-1] if rounds else None
            contributions = await self._run_round(
                req, active, round_index, previous, correlation_id, base_depth, on_progress
            )
            rounds.append(DebateRound(index=round_index, contributions=contributions))
            survivors = {c.label for c in contributions if c.ok}
            active = [voice for voice in active if voice.label in survivors]

        final = await self._synthesize_final(req, rounds, correlation_id, base_depth, on_progress)
        return DebateResult(prompt=req.prompt, rounds=rounds, final=final)

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
        return [
            _Voice(
                index=index,
                target=target,
                label=_label(target),
                stance=req.stances[index] if req.stances else None,
            )
            for index, target in enumerate(req.targets)
        ]

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
    ) -> list[DebateContribution]:
        """Run one round: every active voice answers (round 1) or rebuts (later rounds) in parallel."""

        async def one(voice: _Voice) -> DebateContribution:
            prompt = self._round_prompt(req, voice, previous)
            request = DelegationRequest(
                target=voice.target,
                prompt=prompt,
                working_dir=req.working_dir,
                files=req.files,
                role=req.role,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
                include_raw=req.include_raw,
                depth=base_depth,
            )
            result = await self._delegation.delegate(
                request,
                correlation_id=f"{correlation_id}:r{round_index}:{voice.index}",
                base_depth=base_depth,
                on_progress=on_progress,
            )
            return _to_contribution(voice, round_index, result)

        return list(await asyncio.gather(*(one(voice) for voice in voices)))

    def _round_prompt(self, req: DebateRequest, voice: _Voice, previous: DebateRound | None) -> str:
        """Build the prompt for ``voice`` this round: a fresh answer, or a rebuttal of the others."""
        if previous is None:
            return _apply_stance(req.prompt, voice.stance)
        own = _latest_text(previous, voice.label)
        others = [
            (contribution.label, contribution.text)
            for contribution in previous.contributions
            if contribution.label != voice.label and contribution.ok and contribution.text.strip()
        ]
        return _rebuttal_prompt(req.prompt, own, others, voice.stance)

    async def _synthesize_final(
        self,
        req: DebateRequest,
        rounds: list[DebateRound],
        correlation_id: str,
        base_depth: int,
        on_progress: ProgressCallback | None,
    ) -> str | None:
        """Delegate a closing pass over the final positions, stating where the panel landed."""
        if not req.synthesize or not rounds:
            return None
        final_round = rounds[-1]
        closing = [c for c in final_round.contributions if c.ok and c.text.strip()]
        if not closing:
            return None
        _announce(on_progress, "debate: synthesizing the closing statement")
        transcript = "\n\n".join(f"## {c.label}\n{c.text}" for c in closing)
        prompt = (
            "You are closing out a debate among several AI coding agents on the same question.\n\n"
            f"The question:\n{req.prompt}\n\n"
            f"Their final positions:\n\n{transcript}\n\n"
            "Write the closing summary: state where they converged, lay out the remaining "
            "disagreements and the strongest case on each side, and give your best overall answer."
        )
        synth_request = DelegationRequest(
            target=closing[0].target,
            prompt=prompt,
            working_dir=req.working_dir,
            safety_mode=SafetyMode.READ_ONLY,
            timeout_s=req.timeout_s,
            depth=base_depth + 1,
        )
        result = await self._delegation.delegate(
            synth_request,
            correlation_id=f"{correlation_id}:final",
            base_depth=base_depth + 1,
        )
        return result.text if result.ok else None


def _label(target: Target) -> str:
    """The transcript key for a voice: ``cli:model``, or just ``cli`` at the adapter's default."""
    return f"{target.cli}:{target.model}" if target.model else target.cli


def _latest_text(round_: DebateRound, label: str) -> str:
    """Return ``label``'s answer text from a round, or empty if it did not contribute."""
    for contribution in round_.contributions:
        if contribution.label == label:
            return contribution.text
    return ""


def _to_contribution(voice: _Voice, round_index: int, result: DelegationResult) -> DebateContribution:
    """Fold a delegation result into a transcript contribution for ``voice``."""
    return DebateContribution(
        label=voice.label,
        target=result.target,
        round_index=round_index,
        stance=voice.stance,
        role=None,
        ok=result.ok,
        text=result.text,
        raw=result.raw,
        duration_s=result.duration_s,
        error=result.error,
        fallback_from=result.fallback_from,
    )


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


def _apply_stance(prompt: str, stance: Stance | None) -> str:
    """Wrap the round-one prompt to steer a voice for or against the proposition."""
    if stance is None or stance is Stance.NEUTRAL:
        return prompt
    if stance is Stance.FOR:
        return f"Argue in favor of the following proposition, making the strongest case for it:\n\n{prompt}"
    return f"Argue against the following proposition, making the strongest case against it:\n\n{prompt}"


def _announce(on_progress: ProgressCallback | None, message: str) -> None:
    """Emit a progress line if a callback is listening (surfaced for async jobs via job_status)."""
    if on_progress is not None:
        on_progress(message)
