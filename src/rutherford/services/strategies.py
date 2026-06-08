# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Consensus strategies: turn a panel of free-text answers into one structured outcome.

A strategy other than ``all-voices`` asks each voice for a verdict and aggregates the verdicts. The
verdict is read in one of two modes: with a ``verdict_schema`` each voice is asked to emit a JSON
object containing a ``verdict`` field (parsed with a balanced-brace scan, so a nested object is read
whole); without one, a final ``VERDICT: <token>`` line is read. A voice whose answer yields no verdict
is excluded from the tally but its reason is recorded by the caller, so the exclusion is never silent.

The aggregation counts against **every eligible voice**, not just the parseable survivors: a failed or
unparseable voice stays in the denominator, so a panel that loses most voices to failure cannot certify
an outcome off the one that answered. ``majority``/``weighted`` require a true >50% share; ``plurality``
is the looser top-scorer rule; ``unanimous`` requires every voice to have weighed in and agreed; and a
panel below ``min_quorum`` parseable voices is ``no_quorum``. Everything here is pure (no I/O), so the
outcome math is unit-testable on its own.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from ..domain.enums import Strategy
from ..domain.models import VoiceVerdict
from ..io.jsontext import last_json_object

#: Matches a ``VERDICT: <token>`` line anywhere in an answer; the last match wins.
_VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def verdict_instruction(schema: dict[str, Any] | None) -> str:
    """The instruction appended to each voice's prompt telling it how to emit its verdict."""
    if schema is not None:
        return (
            "When you have finished, output on its own final line a single JSON object that matches "
            f'this schema and includes a "verdict" field: {json.dumps(schema)}'
        )
    return (
        "When you have finished, output a final line in exactly this form, with your one-word verdict: VERDICT: <token>"
    )


def extract_verdict(text: str, schema: dict[str, Any] | None) -> str | None:
    """Pull a normalized verdict token out of a voice's answer, or ``None`` if there is none."""
    if schema is not None:
        return _verdict_from_json(text)
    return _verdict_from_line(text)


def _verdict_from_line(text: str) -> str | None:
    matches = _VERDICT_LINE.findall(text)
    return _normalize(matches[-1]) if matches else None


def _verdict_from_json(text: str) -> str | None:
    obj = last_json_object(text)
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    return _normalize(verdict) if isinstance(verdict, str) and verdict.strip() else None


def _normalize(token: str) -> str:
    return token.strip().lower()


def aggregate(strategy: Strategy, voices: list[VoiceVerdict], *, min_quorum: int = 1) -> tuple[str, str | None]:
    """Aggregate verdicts into ``(outcome, decision)`` for ``strategy``.

    ``voices`` is the whole panel (including failed/unparseable voices); those stay in the denominator
    so an outcome cannot be certified off a minority of survivors. ``min_quorum`` is the floor on
    parseable voices below which the outcome is ``no_quorum``. ``decision`` is the winning verdict token
    when one was reached, else ``None``. ``all-voices`` never reaches here.

    Outcomes: ``unanimous`` | ``majority`` | ``no_majority`` | ``plurality`` | ``tied`` | ``split`` |
    ``agree`` | ``escalate`` | ``no_quorum``.
    """
    parseable = [voice for voice in voices if voice.ok and voice.verdict is not None]
    eligible = len(voices)
    if len(parseable) < min_quorum:
        return ("no_quorum", None)
    if strategy is Strategy.UNANIMOUS:
        return _unanimous(parseable, eligible)
    if strategy is Strategy.MAJORITY:
        return _strict_majority(Counter(str(voice.verdict) for voice in parseable), float(eligible))
    if strategy is Strategy.PLURALITY:
        return _plurality(Counter(str(voice.verdict) for voice in parseable))
    if strategy is Strategy.WEIGHTED:
        sums: dict[str, float] = defaultdict(float)
        for voice in parseable:
            sums[str(voice.verdict)] += voice.weight
        total_weight = sum(voice.weight for voice in voices)  # failed voices keep their weight in the denominator
        return _strict_majority(sums, total_weight)
    if strategy is Strategy.PARITY_PAIR:
        return _parity_pair(voices, parseable)
    return ("split", None)  # defensive; ALL_VOICES does not aggregate


def _unanimous(parseable: list[VoiceVerdict], eligible: int) -> tuple[str, str | None]:
    """Unanimity requires every eligible voice to have weighed in and agreed; a failure vetoes it."""
    if len(parseable) < eligible:
        return ("split", None)  # a failed or unparseable voice means the panel did not all agree
    verdicts = {voice.verdict for voice in parseable}
    if len(verdicts) == 1:
        return ("unanimous", parseable[0].verdict)
    return ("split", None)


def _strict_majority(scores: dict[Any, float] | Counter[Any], total: float) -> tuple[str, str | None]:
    """A verdict wins only if its score strictly exceeds half of ``total`` (a true majority)."""
    if not scores or total <= 0:
        return ("no_majority", None)
    top_verdict, top_score = max(scores.items(), key=lambda item: item[1])
    if top_score > total / 2:
        return ("majority", str(top_verdict))
    return ("no_majority", None)


def _plurality(scores: dict[Any, float] | Counter[Any]) -> tuple[str, str | None]:
    """The single highest-scoring verdict wins even below 50%; a tie at the top is ``tied``."""
    if not scores:
        return ("tied", None)
    top = max(scores.values())
    leaders = [verdict for verdict, score in scores.items() if _close(score, top)]
    if len(leaders) == 1:
        return ("plurality", str(leaders[0]))
    return ("tied", None)


def _parity_pair(voices: list[VoiceVerdict], parseable: list[VoiceVerdict]) -> tuple[str, str | None]:
    """The proposer's verdict must match every parity counterweight, or the panel escalates."""
    proposer = _find_proposer(voices)
    parity_voices = [voice for voice in parseable if voice.parity]
    if proposer is None or proposer.verdict is None or not parity_voices:
        return ("escalate", None)
    if {voice.verdict for voice in parity_voices} == {proposer.verdict}:
        return ("agree", proposer.verdict)
    return ("escalate", None)


def _find_proposer(voices: list[VoiceVerdict]) -> VoiceVerdict | None:
    """The proposer seat: one labeled ``proposer``, else the heaviest non-parity voice."""
    labeled = [voice for voice in voices if voice.label == "proposer"]
    if labeled:
        return labeled[0]
    non_parity = [voice for voice in voices if not voice.parity]
    if not non_parity:
        return None
    return max(non_parity, key=lambda voice: voice.weight)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9
