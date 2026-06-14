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
from dataclasses import dataclass
from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession, run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import SafetyMode, Stance, is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    DebateContribution,
    DebateRequest,
    DebateResult,
    DebateRound,
    DelegationResult,
    ErrorInfo,
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
        """Open one session per voice, run up to ``rounds`` rounds (delta prompts after round 1), and close."""
        voices = self._resolve_voices(req)
        rounds_cap = self._resolve_rounds(req)
        cwd = req.working_dir or str(Path.cwd())
        policy = PermissionPolicy(mode=req.safety_mode)
        if is_mutating(req.safety_mode) and not req.working_dir:
            raise RutherfordError(ErrorCode.WORKSPACE_NOT_TRUSTED, f"{req.safety_mode.value} mode needs a working_dir")
        timeout_s = req.timeout_s or self._config.default_timeout_s

        sessions: dict[int, ACPSession] = {}
        open_errors: dict[int, str] = {}
        await self._open_sessions(voices, policy, cwd, sessions, open_errors)
        try:
            rounds: list[DebateRound] = []
            active = [voice for voice in voices if voice.index in sessions]
            for round_index in range(1, rounds_cap + 1):
                if round_index > 1 and len(active) < 2:
                    break  # a debate needs at least two voices to keep arguing
                previous = rounds[-1] if rounds else None
                contributions = await self._run_round(
                    req, voices, active, sessions, open_errors, round_index, previous, timeout_s
                )
                rounds.append(DebateRound(index=round_index, contributions=contributions))
                survivors = {c.seat_id for c in contributions if c.ok and c.text.strip()}
                active = [voice for voice in active if _seat_id(voice) in survivors]
            final, synthesis_by = await self._synthesize(req, rounds, cwd, timeout_s)
            return DebateResult(prompt=req.prompt, rounds=rounds, final=final, synthesis_by=synthesis_by)
        finally:
            await asyncio.gather(*(session.close() for session in sessions.values()), return_exceptions=True)

    async def _open_sessions(
        self,
        voices: list[_Voice],
        policy: PermissionPolicy,
        cwd: str,
        sessions: dict[int, ACPSession],
        open_errors: dict[int, str],
    ) -> None:
        """Open one ACP session per voice in parallel; record an unknown-agent or handshake failure."""

        async def _open(voice: _Voice) -> None:
            if not self._descriptors.has(voice.target.cli):
                open_errors[voice.index] = f"unknown agent id {voice.target.cli!r}"
                return
            session = ACPSession(
                self._descriptors.get(voice.target.cli), policy=policy, cwd=cwd, model=voice.target.model
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
    ) -> list[DebateContribution]:
        """Run one round: every active voice answers (round 1) or rebuts the others (later rounds), in parallel.

        Round 1 also emits a failed contribution for any voice whose session never opened, so the transcript
        shows where a voice fell out. Later rounds run only the surviving active voices.
        """

        async def _turn(voice: _Voice) -> DebateContribution:
            prompt = self._round_prompt(req, voice, previous)
            result = await sessions[voice.index].prompt(prompt, timeout_s=timeout_s)
            return _to_contribution(voice, round_index, result)

        contributions = list(await asyncio.gather(*(_turn(voice) for voice in active)))
        if round_index == 1:
            for voice in voices:
                if voice.index in open_errors:
                    contributions.append(_failed_contribution(voice, round_index, open_errors[voice.index]))
        contributions.sort(key=lambda contribution: contribution.seat_id)
        return contributions

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


def _last_usable_round(rounds: list[DebateRound]) -> DebateRound | None:
    for round_ in reversed(rounds):
        if any(c.ok and c.text.strip() for c in round_.contributions):
            return round_
    return None
