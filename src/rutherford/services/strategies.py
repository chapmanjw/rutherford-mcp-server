# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Consensus strategies: turn a panel of free-text answers into one structured outcome.

A strategy other than ``all-voices`` asks each voice for a verdict and aggregates the verdicts. The
verdict is read in one of two modes: with a ``verdict_schema`` each voice is asked to emit a JSON
object containing a ``verdict`` field; without one, a final ``VERDICT: <token>`` line is read. A
voice whose answer yields no verdict is ``unparseable`` -- still returned, but excluded from the
tally. Everything here is pure (no I/O), so the outcome math is unit-testable on its own.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from ..domain.enums import Strategy
from ..domain.models import VoiceVerdict

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
    obj = _last_json_object(text)
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    return _normalize(verdict) if isinstance(verdict, str) and verdict.strip() else None


def _last_json_object(text: str) -> Any:
    """Parse the last ``{...}`` block in ``text`` as JSON, or return ``None`` if none parses."""
    for match in reversed(list(re.finditer(r"\{.*?\}", text, re.DOTALL))):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    return None


def _normalize(token: str) -> str:
    return token.strip().lower()


def aggregate(strategy: Strategy, voices: list[VoiceVerdict]) -> tuple[str, str | None]:
    """Aggregate verdicts into ``(outcome, decision)`` for ``strategy``.

    Only voices that produced a verdict take part; ``decision`` is the winning verdict token when one
    was reached, else ``None``. ``all-voices`` never reaches here.
    """
    parseable = [voice for voice in voices if voice.ok and voice.verdict is not None]
    if strategy is Strategy.UNANIMOUS:
        return _unanimous(parseable)
    if strategy is Strategy.MAJORITY:
        return _plurality(Counter(voice.verdict for voice in parseable))
    if strategy is Strategy.WEIGHTED:
        sums: dict[str, float] = defaultdict(float)
        for voice in parseable:
            sums[str(voice.verdict)] += voice.weight
        return _plurality(sums)
    if strategy is Strategy.PARITY_PAIR:
        return _parity_pair(voices, parseable)
    return ("split", None)  # defensive; ALL_VOICES does not aggregate


def _unanimous(parseable: list[VoiceVerdict]) -> tuple[str, str | None]:
    if not parseable:
        return ("split", None)
    verdicts = {voice.verdict for voice in parseable}
    if len(verdicts) == 1:
        return ("unanimous", parseable[0].verdict)
    return ("split", None)


def _plurality(scores: dict[Any, float] | Counter[Any]) -> tuple[str, str | None]:
    """A single highest-scoring verdict wins (``majority``); a tie at the top is ``tied``."""
    if not scores:
        return ("tied", None)
    top = max(scores.values())
    leaders = [verdict for verdict, score in scores.items() if _close(score, top)]
    if len(leaders) == 1:
        return ("majority", str(leaders[0]))
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
