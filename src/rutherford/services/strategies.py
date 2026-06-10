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
from collections.abc import Sequence
from typing import Any

from ..domain.enums import Strategy
from ..domain.models import DiversityReport, Provenance, VoiceVerdict
from ..io.jsontext import iter_json_objects

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
    """Return the verdict from the last JSON object that carries a usable ``verdict`` string.

    Not merely the last object: a model often appends a trailing footer object (token usage, a
    "done" marker) after its verdict object, so scan every object and keep the last one whose
    ``verdict`` is a non-empty string. A trailing footer without a ``verdict`` no longer shadows the
    vote (which would otherwise mis-record the voice as ``unparseable``).
    """
    chosen: str | None = None
    for obj in iter_json_objects(text):
        verdict = obj.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            chosen = verdict
    return _normalize(chosen) if chosen is not None else None


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
        return _parity_pair(voices)
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
    """The single highest-scoring verdict wins even below 50%; a tie at the top is ``tied``.

    This is the deliberately lenient rule: unlike ``majority``/``weighted`` it does not require a
    >50% share, so it can certify off a minority of the parseable voices. The only floor on it is
    ``min_quorum`` (applied in :func:`aggregate`); raise that to refuse a thin plurality.
    """
    if not scores:
        return ("tied", None)
    top = max(scores.values())
    leaders = [verdict for verdict, score in scores.items() if _close(score, top)]
    if len(leaders) == 1:
        return ("plurality", str(leaders[0]))
    return ("tied", None)


def _parity_pair(voices: list[VoiceVerdict]) -> tuple[str, str | None]:
    """The proposer's verdict must match every parity counterweight, or the panel escalates.

    Every parity seat must weigh in: a counterweight that failed or produced no verdict is a
    non-answer that cannot corroborate, so it vetoes agreement and the panel escalates -- rather than
    declaring agreement off only the parity voices that happened to parse. This honors the same
    "failed/unparseable voices are not silently dropped" invariant as the counting strategies.
    """
    proposer = _find_proposer(voices)
    parity_voices = [voice for voice in voices if voice.parity]
    if proposer is None or proposer.verdict is None or not parity_voices:
        return ("escalate", None)
    if any(not voice.ok or voice.verdict is None for voice in parity_voices):
        return ("escalate", None)  # a counterweight that did not answer cannot corroborate
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


def effective_diversity(provenances: Sequence[Provenance | None], *, min_distinct: int = 2) -> DiversityReport:
    """Summarize how many distinct models/providers a panel's *answering* voices actually spanned (F3).

    ``provenances`` is the provenance of every voice that produced an answer (failed voices, which
    contributed no opinion, are excluded by the caller). A voice whose model (or provider) could not be
    resolved is excluded from that distinct tally rather than assumed same or different; a model that
    could not be resolved is also counted in ``unknown``.

    ``low_diversity`` flags when, among the voices that resolved, the distinct *model* count OR the
    distinct *provider* (vendor) count collapses below ``min_distinct`` -- both because F3's load-bearing
    case is "one model in N CLI costumes", and the costume can be either the same model under different
    id strings (caught by the vendor axis) or literally the same id (caught by the model axis). An
    all-unknown panel is unmeasured, not flagged. Distinct providers key on the vendor, not the serving
    backend, so the same model served two ways is one provider. Pure, so the math is unit-testable.
    """
    models: set[str] = set()
    providers: set[str] = set()
    unknown = 0
    known_providers = 0
    for prov in provenances:
        model_key = _model_key(prov)
        if model_key is None:
            unknown += 1
        else:
            models.add(model_key)
        provider_key = _provider_key(prov)
        if provider_key is not None:
            known_providers += 1
            providers.add(provider_key)
    known_models = len(provenances) - unknown
    low_diversity = (known_models >= 2 and len(models) < min_distinct) or (
        known_providers >= 2 and len(providers) < min_distinct
    )
    return DiversityReport(
        answered_voices=len(provenances),
        distinct_models=len(models),
        distinct_providers=len(providers),
        unknown=unknown,
        low_diversity=low_diversity,
        models=sorted(models),
        providers=sorted(providers),
    )


def _model_key(prov: Provenance | None) -> str | None:
    """The diversity key for a voice's model: its normalized model id, or ``None`` when unresolved."""
    if prov is None or not prov.model:
        return None
    return prov.model.strip().lower()


def _provider_key(prov: Provenance | None) -> str | None:
    """The diversity key for a voice's provider (the vendor, not the serving backend), or ``None``."""
    if prov is None or not prov.provider:
        return None
    return prov.provider.strip().lower()
