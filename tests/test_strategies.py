# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the pure consensus-strategy logic: verdict extraction and aggregation."""

from __future__ import annotations

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


def test_extract_verdict_json_without_verdict_field_is_none() -> None:
    schema = {"type": "object"}
    assert extract_verdict('{"answer": "yes"}', schema) is None


def test_extract_verdict_invalid_json_is_none() -> None:
    schema = {"type": "object"}
    assert extract_verdict("not json at all", schema) is None


def test_verdict_instruction_differs_by_mode() -> None:
    assert "VERDICT:" in verdict_instruction(None)
    assert "JSON" in verdict_instruction({"type": "object"})


# --- aggregation --------------------------------------------------------------------------------


def test_unanimous_agrees_and_splits() -> None:
    assert aggregate(Strategy.UNANIMOUS, [_voice("a", "approve"), _voice("b", "approve")]) == ("unanimous", "approve")
    assert aggregate(Strategy.UNANIMOUS, [_voice("a", "approve"), _voice("b", "block")]) == ("split", None)


def test_unanimous_with_no_verdicts_is_split() -> None:
    assert aggregate(Strategy.UNANIMOUS, [_voice("a", None)]) == ("split", None)


def test_majority_counts_votes_and_ties() -> None:
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block")]
    assert aggregate(Strategy.MAJORITY, voices) == ("majority", "approve")
    tied = [_voice("a", "approve"), _voice("b", "block")]
    assert aggregate(Strategy.MAJORITY, tied) == ("tied", None)


def test_majority_ignores_weight() -> None:
    # Two light "approve" votes beat one heavy "block" by count.
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block", weight=10.0)]
    assert aggregate(Strategy.MAJORITY, voices) == ("majority", "approve")


def test_weighted_uses_summed_weight() -> None:
    # One heavy "block" outweighs two light "approve" votes.
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", "block", weight=5.0)]
    assert aggregate(Strategy.WEIGHTED, voices) == ("majority", "block")


def test_weighted_ties() -> None:
    voices = [_voice("a", "approve", weight=2.0), _voice("b", "block", weight=2.0)]
    assert aggregate(Strategy.WEIGHTED, voices) == ("tied", None)


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


def test_unparseable_voices_are_excluded_from_the_tally() -> None:
    voices = [_voice("a", "approve"), _voice("b", "approve"), _voice("c", None)]
    assert aggregate(Strategy.UNANIMOUS, voices) == ("unanimous", "approve")
