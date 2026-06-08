# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the pure consensus-strategy logic: verdict extraction and aggregation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rutherford.domain.enums import Strategy
from rutherford.domain.models import VoiceVerdict
from rutherford.services.strategies import aggregate, extract_verdict, verdict_instruction


def _voice(
    label: str, verdict: str | None, *, weight: float = 1.0, parity: bool = False, ok: bool = True
) -> VoiceVerdict:
    return VoiceVerdict(label=label, cli=label, verdict=verdict, weight=weight, parity=parity, ok=ok)


# --- verdict extraction -------------------------------------------------------------------------


def test_extract_verdict_from_final_line() -> None:
    assert extract_verdict("here is my reasoning\nVERDICT: Approve", None) == "approve"


def test_extract_verdict_takes_the_last_line() -> None:
    assert extract_verdict("VERDICT: maybe\nmore\nVERDICT: block", None) == "block"


def test_extract_verdict_missing_line_is_none() -> None:
    assert extract_verdict("no verdict here", None) is None


def test_extract_verdict_from_json_when_schema_given() -> None:
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    assert extract_verdict('reasoning...\n{"verdict": "Block", "why": "risky"}', schema) == "block"


def test_extract_verdict_from_nested_json_object() -> None:
    # The fixed bug: a verdict object with an object-valued field must still parse. The old
    # non-greedy `\{.*?\}` regex stopped at the first '}', dropping the whole verdict.
    schema = {"type": "object"}
    text = 'reasoning...\n{"verdict": "Block", "evidence": {"file": "x.py", "line": 12}}'
    assert extract_verdict(text, schema) == "block"


def test_extract_verdict_json_without_verdict_field_is_none() -> None:
    schema = {"type": "object"}
    assert extract_verdict('{"answer": "yes"}', schema) is None


def test_extract_verdict_invalid_json_is_none() -> None:
    schema = {"type": "object"}
    assert extract_verdict("not json at all", schema) is None


def test_extract_verdict_ignores_a_trailing_array_of_objects() -> None:
    # The drift the bulletproofing audit caught: a verdict object followed by a trailing array of
    # objects (a "related files" list). The scanner does not descend into the array, so the real
    # verdict still wins instead of being shadowed by the last array element.
    schema = {"type": "object"}
    text = '{"verdict": "block"}\nRelated: [{"file": "a.py"}, {"verdict": "approve"}]'
    assert extract_verdict(text, schema) == "block"


def test_extract_verdict_prefers_the_last_object_that_has_a_verdict() -> None:
    # A trailing footer object (token usage, a "done" marker) without a verdict must not shadow the
    # real verdict and mis-record the voice as unparseable.
    schema = {"type": "object"}
    text = '{"verdict": "approve"}\n{"tokens": 42, "done": true}'
    assert extract_verdict(text, schema) == "approve"


def test_voice_verdict_rejects_a_negative_weight() -> None:
    # A negative weight would shrink the strategy denominator and let one voice fake a majority.
    with pytest.raises(ValidationError):
        VoiceVerdict(label="a", cli="a", verdict="approve", weight=-1.0)


def test_verdict_instruction_differs_by_mode() -> None:
    assert "VERDICT:" in verdict_instruction(None)
    assert "JSON" in verdict_instruction({"type": "object"})


# --- aggregation --------------------------------------------------------------------------------


def test_unanimous_agrees_and_splits() -> None:
    assert aggregate(Strategy.UNANIMOUS, [_voice("a", "approve"), _voice("b", "approve")]) == ("unanimous", "approve")
    assert aggregate(Strategy.UNANIMOUS, [_voice("a", "approve"), _voice("b", "block")]) == ("split", None)


def test_unanimous_with_no_verdicts_is_no_quorum() -> None:
    # No parseable voice cannot certify anything (default min_quorum is 1).
    assert aggregate(Strategy.UNANIMOUS, [_voice("a", None)]) == ("no_quorum", None)


def test_unanimous_vetoed_by_a_failed_or_unparseable_voice() -> None:
    # The fixed quorum-of-one bug: every eligible voice must weigh in, so an unparseable or failed
    # voice forces a split instead of certifying unanimity off the survivors.
    mixed = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", None)]
    assert aggregate(Strategy.UNANIMOUS, mixed) == ("split", None)
    one_of_eight = [_voice("a", "approve")] + [_voice(f"f{i}", None, ok=False) for i in range(7)]
    assert aggregate(Strategy.UNANIMOUS, one_of_eight) == ("split", None)


def test_majority_requires_over_half_of_all_eligible_voices() -> None:
    assert aggregate(Strategy.MAJORITY, [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block")]) == (
        "majority",
        "approve",
    )  # 2 of 3
    plurality = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block"), _voice("d", "escalate")]
    assert aggregate(Strategy.MAJORITY, plurality) == ("no_majority", None)  # 2 of 4 is not > 50%
    tied = [_voice("a", "approve"), _voice("b", "block")]
    assert aggregate(Strategy.MAJORITY, tied) == ("no_majority", None)


def test_majority_counts_failed_voices_in_the_denominator() -> None:
    # One "approve" plus seven failures is not a majority of eight (the quorum-of-one bug).
    voices = [_voice("a", "approve")] + [_voice(f"f{i}", None, ok=False) for i in range(7)]
    assert aggregate(Strategy.MAJORITY, voices) == ("no_majority", None)


def test_majority_ignores_weight() -> None:
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block", weight=10.0)]
    assert aggregate(Strategy.MAJORITY, voices) == ("majority", "approve")


def test_plurality_wins_below_half_and_ties() -> None:
    # The pre-1.x "majority" behavior, now named plurality: top scorer wins even under 50%.
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block"), _voice("d", "escalate")]
    assert aggregate(Strategy.PLURALITY, voices) == ("plurality", "approve")  # 2 of 4 still wins
    tied = [_voice("a", "approve"), _voice("b", "block")]
    assert aggregate(Strategy.PLURALITY, tied) == ("tied", None)


def test_weighted_uses_summed_weight() -> None:
    # One heavy "block" carries a true majority of weight (5 of 7).
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block", weight=5.0)]
    assert aggregate(Strategy.WEIGHTED, voices) == ("majority", "block")


def test_weighted_requires_over_half_of_total_weight() -> None:
    voices = [_voice("a", "approve", weight=2.0), _voice("b", "block", weight=2.0)]
    assert aggregate(Strategy.WEIGHTED, voices) == ("no_majority", None)  # 2 of 4 is not > 50%


def test_weighted_counts_failed_voice_weight_in_the_denominator() -> None:
    voices = [
        _voice("a", "approve", weight=3.0),
        _voice("b", "block", weight=2.0),
        _voice("c", None, ok=False, weight=4.0),
    ]
    assert aggregate(Strategy.WEIGHTED, voices) == ("no_majority", None)  # approve 3 of 9 weight


def test_min_quorum_floor_blocks_a_thin_panel() -> None:
    voices = [_voice("a", "approve"), _voice("b", None, ok=False)]
    assert aggregate(Strategy.MAJORITY, voices, min_quorum=2) == ("no_quorum", None)
    assert aggregate(Strategy.UNANIMOUS, voices, min_quorum=2) == ("no_quorum", None)


def test_parity_pair_escalates_on_disagreement() -> None:
    voices = [_voice("proposer", "approve"), _voice("dissenter", "block", parity=True)]
    assert aggregate(Strategy.PARITY_PAIR, voices) == ("escalate", None)


def test_parity_pair_agrees_when_proposer_matches_parity() -> None:
    voices = [_voice("proposer", "approve"), _voice("dissenter", "approve", parity=True)]
    assert aggregate(Strategy.PARITY_PAIR, voices) == ("agree", "approve")


def test_parity_pair_without_a_parity_seat_escalates() -> None:
    voices = [_voice("proposer", "approve"), _voice("other", "approve")]
    assert aggregate(Strategy.PARITY_PAIR, voices) == ("escalate", None)


def test_parity_pair_falls_back_to_heaviest_non_parity_proposer() -> None:
    # No seat labeled "proposer": the heaviest non-parity voice plays that role.
    voices = [_voice("a", "approve", weight=3.0), _voice("b", "approve", parity=True)]
    assert aggregate(Strategy.PARITY_PAIR, voices) == ("agree", "approve")


def test_parity_pair_escalates_when_a_counterweight_fails() -> None:
    # The bulletproofing fix: a parity counterweight that failed or produced no verdict cannot
    # corroborate, so the panel escalates rather than agreeing off only the surviving counterweights.
    voices = [
        _voice("proposer", "approve"),
        _voice("d1", "approve", parity=True),
        _voice("d2", None, ok=False, parity=True),
    ]
    assert aggregate(Strategy.PARITY_PAIR, voices) == ("escalate", None)
