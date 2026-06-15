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

from ..domain.enums import Stance, Strategy
from ..domain.models import (
    DiversityReport,
    PairwiseAgreement,
    Provenance,
    RankEntry,
    RankReport,
    VoiceVerdict,
)
from ..io.jsontext import iter_json_objects

#: Matches a ``VERDICT: <token>`` line anywhere in an answer; the last match wins.
_VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
#: Matches a ``RANK: A, C, B`` line anywhere in a ballot answer; the last match wins.
_RANK_LINE = re.compile(r"^\s*RANK:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def apply_stance(prompt: str, stance: Stance | None) -> str:
    """Wrap the prompt to steer a voice for or against the proposition.

    The one steering wrapper shared by consensus and the opening debate round, so both panels
    phrase the same stance identically.
    """
    if stance is None or stance is Stance.NEUTRAL:
        return prompt
    if stance is Stance.FOR:
        return f"Argue in favor of the following proposition, making the strongest case for it:\n\n{prompt}"
    return f"Argue against the following proposition, making the strongest case against it:\n\n{prompt}"


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


# --- RANK: the two-round preference protocol (F4b) ---------------------------


def ranking_instruction(labels: Sequence[str], schema: dict[str, Any] | None) -> str:
    """Tell a voice how to rank the labelled candidate answers (line mode, or JSON when a schema is set)."""
    label_list = ", ".join(labels)
    if schema is not None:
        return (
            f"Rank ALL of these answers from best to worst by their labels ({label_list}). When you are "
            "finished, output on its own final line a single JSON object of the form "
            '{"ranking": ["<best label>", ..., "<worst label>"]}, listing every label exactly once.'
        )
    return (
        f"Rank ALL of these answers from best to worst by their labels ({label_list}). When you are "
        "finished, output a final line in exactly this form, most preferred first: RANK: <label>, <label>, ..."
    )


def extract_ranking(text: str, schema: dict[str, Any] | None, valid_labels: Sequence[str]) -> list[str] | None:
    """Pull an ordered list of candidate labels (best to worst) out of a ballot answer, or ``None``.

    Two modes mirror :func:`extract_verdict`: with a ``schema`` the last JSON object carrying a
    ``ranking`` / ``rank`` / ``order`` list wins; otherwise the last ``RANK:`` line is read. Either way
    the result is normalized to the known ``valid_labels`` (case-insensitively), de-duplicated keeping
    first occurrence, and unknown tokens are dropped -- so a hallucinated label never enters the tally.
    ``None`` when no usable ranking is found (the caller records the ballot as unparseable).
    """
    valid = {label.upper() for label in valid_labels}
    raw = _ranking_from_json(text) if schema is not None else _ranking_from_line(text)
    if raw is None:
        return None
    return _dedupe_valid(raw, valid)


def _ranking_from_line(text: str) -> list[str] | None:
    matches = _RANK_LINE.findall(text)
    if not matches:
        return None
    return re.split(r"[,\s]+", matches[-1].strip())


def _ranking_from_json(text: str) -> list[str] | None:
    """The ordered labels from the last JSON object carrying a ``ranking`` / ``rank`` / ``order`` list."""
    chosen: list[Any] | None = None
    for obj in iter_json_objects(text):
        for key in ("ranking", "rank", "order"):
            value = obj.get(key)
            if isinstance(value, list) and value:
                chosen = value
    return [str(item) for item in chosen] if chosen is not None else None


def _dedupe_valid(tokens: Sequence[str], valid: set[str]) -> list[str] | None:
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.strip().upper()
        if key in valid and key not in seen:
            seen.add(key)
            out.append(key)
    return out or None


def rank_panel(
    candidates: Sequence[tuple[str, str]],
    ballots: Sequence[tuple[str, Sequence[str]]],
    *,
    min_quorum: int = 1,
) -> tuple[str, str | None, RankReport]:
    """Aggregate ranking ballots into ``(outcome, decision, RankReport)`` by Borda mean-rank (F4b, 7-F).

    ``candidates`` is ``(label, cli)`` for every answer being ranked; ``ballots`` is ``(voter_label,
    ordered candidate labels best to worst)`` -- each voter's ballot excludes its own answer (7-E), so a
    candidate is ranked by every voter but itself. The leaderboard sorts by mean ballot position ascending
    (a Borda-points and label tiebreak keeps it deterministic); the winner is the single best unless the
    top is an epsilon tie (then ``tied`` with no decision). ``pairwise`` is the Spearman matrix and
    ``concordance`` the mean pairwise correlation. Below ``min_quorum`` usable ballots the outcome is
    ``no_quorum``. Pure, so the ranking math is unit-testable on its own.
    """
    cand_labels = [label for label, _ in candidates]
    cand_set = set(cand_labels)
    cli_of = dict(candidates)
    usable = [(voter, [c for c in order if c in cand_set]) for voter, order in ballots]
    usable = [(voter, order) for voter, order in usable if order]
    ballots_cast = len(usable)
    ballots_unparseable = len(ballots) - ballots_cast
    if ballots_cast < min_quorum:
        return ("no_quorum", None, RankReport(ballots_cast=ballots_cast, ballots_unparseable=ballots_unparseable))

    positions: dict[str, list[int]] = {label: [] for label in cand_labels}
    points: dict[str, float] = dict.fromkeys(cand_labels, 0.0)
    for _voter, order in usable:
        length = len(order)
        for index, label in enumerate(order):  # index 0 = the voter's top pick
            positions[label].append(index + 1)
            points[label] += length - index  # top of an L-candidate ballot earns L, bottom earns 1
    bottom = float(len(cand_labels) + 1)  # an answer no surviving ballot ranked sinks below every ranked one
    scored = [
        (
            label,
            sum(positions[label]) / len(positions[label]) if positions[label] else bottom,
            points[label],
            len(positions[label]),
        )
        for label in cand_labels
    ]
    scored.sort(key=lambda entry: (entry[1], -entry[2], entry[0]))
    leaderboard = [
        RankEntry(label=label, cli=cli_of[label], rank=index + 1, mean_rank=round(mean, 4), borda_points=pts, ballots=n)
        for index, (label, mean, pts, n) in enumerate(scored)
    ]
    best_mean = scored[0][1]
    tied_top = [label for label, mean, _, _ in scored if _close(mean, best_mean)]
    winner = tied_top[0] if len(tied_top) == 1 else None
    outcome = "ranked" if winner is not None else "tied"
    pairwise = _pairwise_matrix(usable)
    concordance = round(sum(p.correlation for p in pairwise) / len(pairwise), 4) if pairwise else None
    report = RankReport(
        leaderboard=leaderboard,
        winner=winner,
        tied_top=[] if winner is not None else tied_top,
        pairwise=pairwise,
        concordance=concordance,
        ballots_cast=ballots_cast,
        ballots_unparseable=ballots_unparseable,
    )
    return (outcome, winner, report)


def _pairwise_matrix(ballots: Sequence[tuple[str, Sequence[str]]]) -> list[PairwiseAgreement]:
    """The Spearman rank-correlation between every voter pair over the answers they BOTH ranked (7-F).

    The correlation is over each voter's RELATIVE order of the common answers, re-ranked densely to 1..k --
    not the absolute ballot positions. Two voters who agree on the common order must score ``1.0`` even when
    they slotted the non-common answers (a different self-excluded seat each) at different depths; absolute
    positions would let those gaps drag an identical common order below 1.0.
    """
    indexed = [(voter, {label: index + 1 for index, label in enumerate(order)}) for voter, order in ballots]
    out: list[PairwiseAgreement] = []
    for first in range(len(indexed)):
        for second in range(first + 1, len(indexed)):
            a_label, a_pos = indexed[first]
            b_label, b_pos = indexed[second]
            common = [label for label in a_pos if label in b_pos]
            a_dense = _dense_ranks(common, a_pos)
            b_dense = _dense_ranks(common, b_pos)
            corr = _pearson([a_dense[label] for label in common], [b_dense[label] for label in common])
            if corr is None:
                continue  # fewer than two answers in common -- no defined correlation
            out.append(PairwiseAgreement(a=a_label, b=b_label, correlation=round(corr, 4), common=len(common)))
    return out


def _dense_ranks(labels: Sequence[str], positions: dict[str, int]) -> dict[str, int]:
    """Re-rank ``labels`` densely to ``1..k`` by ballot ``positions``, so gaps from other answers drop out."""
    ordered = sorted(labels, key=lambda label: positions[label])
    return {label: rank for rank, label in enumerate(ordered, start=1)}


def _pearson(xs: Sequence[int], ys: Sequence[int]) -> float | None:
    """Pearson correlation of two rank vectors (= Spearman over distinct ranks), or ``None`` if undefined."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None  # a constant vector has no defined correlation
    return float(cov / (var_x * var_y) ** 0.5)


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
        # Effective lineages (item 5): the vendor proxy now -- a NAMED concept the vote-math stretch can later
        # rekey to model-family without changing this field's meaning.
        effective_lineages=len(providers),
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
