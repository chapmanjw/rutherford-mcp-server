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
    DiversityReport,
    ErrorInfo,
    Target,
)
from ..io.ledger import RunLedger
from ..runtime.depth import ensure_within_target_cap
from .delegation import DelegationService, ProgressCallback
from .persistence import PanelVoice, write_panel_record
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
        persist = req.persist if req.persist is not None else (self._config.default_persistence == "job")
        parent_run_id = uuid.uuid4().hex if persist and self._ledger is not None else None

        rounds: list[DebateRound] = []
        # The voices still in the debate; a failed turn removes its voice from later rounds.
        active = list(voices)
        for round_index in range(1, rounds_cap + 1):
            if round_index > 1 and len(active) < 2:
                break  # a debate needs at least two voices to rebut one another
            _announce(on_progress, f"debate: round {round_index} of {rounds_cap} ({len(active)} voices)")
            previous = rounds[-1] if rounds else None
            contributions = await self._run_round(
                req, active, round_index, previous, correlation_id, base_depth, on_progress, parent_run_id
            )
            rounds.append(DebateRound(index=round_index, contributions=contributions))
            survivors = {c.seat_id for c in contributions if c.ok}
            active = [voice for voice in active if voice.seat_id in survivors]

        final, synthesis_by = await self._synthesize_final(req, rounds, correlation_id, base_depth, on_progress)
        result = DebateResult(
            prompt=req.prompt,
            rounds=rounds,
            final=final,
            synthesis_by=synthesis_by,
            diversity=self._diversity(rounds),
        )
        if parent_run_id is not None and self._ledger is not None:
            # Write the parent panel record linking every turn's child record, plus the transcript. The
            # parent's status is derived from the turns (succeeded when any voice ever answered); the
            # transcript.md already inlines every turn, so no separate voices.md is needed here.
            contributions = [c for round_ in rounds for c in round_.contributions]
            clis = sorted({c.target.cli for c in contributions})
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
                extra_artifacts={"transcript.md": _render_transcript(req.prompt, rounds)},
            )
        return result

    def _diversity(self, rounds: list[DebateRound]) -> DiversityReport | None:
        """Effective diversity across the final round's answering voices, or ``None`` if none survived."""
        if not rounds:
            return None
        answered = [c.provenance for c in rounds[-1].contributions if c.ok]
        if not answered:
            return None
        return effective_diversity(answered, min_distinct=self._config.min_distinct)

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
    ) -> list[DebateContribution]:
        """Run one round: every active voice answers (round 1) or rebuts (later rounds) in parallel."""

        async def one(voice: _Voice) -> DebateContribution:
            prompt = self._round_prompt(req, voice, previous)
            request = DelegationRequest(
                target=voice.target,
                prompt=prompt,
                working_dir=req.working_dir,
                files=req.files,
                role=voice.role,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
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
            )
            return _to_contribution(voice, round_index, result)

        # return_exceptions: an exception that still escapes one seat's delegate() must become that
        # seat's failed contribution, not abort the round and discard the other seats' turns.
        outcomes = await asyncio.gather(*(one(voice) for voice in voices), return_exceptions=True)
        contributions: list[DebateContribution] = []
        for voice, outcome in zip(voices, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):  # a real cancellation still propagates
                raise outcome
            if isinstance(outcome, BaseException):
                failed = DelegationResult(
                    target=voice.target,
                    ok=False,
                    error=ErrorInfo(code=ErrorCode.INTERNAL, message=f"voice delegation raised: {outcome!r}"),
                    safety_mode=req.safety_mode,
                )
                contributions.append(_to_contribution(voice, round_index, failed))
            else:
                contributions.append(outcome)
        return contributions

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
        final_round = rounds[-1]
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
        provenance=result.provenance,
        run_dir=result.run_dir,
    )


def _panel_voice(contribution: DebateContribution) -> PanelVoice:
    """Project one debate turn into the panel-parent's :class:`PanelVoice` summary (status + child link)."""
    return PanelVoice(
        label=contribution.label,
        ok=contribution.ok,
        run_id=Path(contribution.run_dir).name if contribution.run_dir else None,
        text=contribution.text,
        error=contribution.error.message if contribution.error else None,
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
