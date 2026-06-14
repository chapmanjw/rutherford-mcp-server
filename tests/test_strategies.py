# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the pure consensus-strategy math: verdict extraction, aggregation, and diversity scoring."""

from __future__ import annotations

from rutherford.domain.enums import Stance, Strategy
from rutherford.domain.models import Provenance, VoiceVerdict
from rutherford.io.jsontext import iter_json_objects, last_json_object
from rutherford.services.strategies import (
    aggregate,
    apply_stance,
    effective_diversity,
    extract_verdict,
    verdict_instruction,
)


def _voice(
    verdict: str | None, *, ok: bool = True, weight: float = 1.0, parity: bool = False, label: str = ""
) -> VoiceVerdict:
    return VoiceVerdict(
        label=label or "v",
        cli="fake",
        weight=weight,
        parity=parity,
        ok=ok,
        verdict=verdict,
        no_verdict_reason=None if (ok and verdict is not None) else ("unparseable" if ok else "failed"),
    )


# --- verdict extraction ------------------------------------------------------


def test_extract_verdict_from_line_last_wins_case_insensitive() -> None:
    text = "First I lean VERDICT: no\nbut on reflection\nverdict: YES"
    assert extract_verdict(text, None) == "yes"


def test_extract_verdict_none_when_no_line() -> None:
    assert extract_verdict("just prose, no verdict here", None) is None


def test_extract_verdict_from_json_last_object_with_verdict() -> None:
    schema = {"verdict": "string"}
    text = 'reasoning... {"verdict": "maybe"} then final {"verdict": "Approve"}\n{"tokens": 42}'
    # the trailing footer object (no verdict) must not shadow the real vote
    assert extract_verdict(text, schema) == "approve"


def test_extract_verdict_json_trailing_array_does_not_steal() -> None:
    schema = {"verdict": "string"}
    text = '{"verdict": "yes"}\nRelated files: [{"verdict": "ignore"}]'
    assert extract_verdict(text, schema) == "yes"


def test_verdict_instruction_shapes() -> None:
    assert "VERDICT: <token>" in verdict_instruction(None)
    schema_text = verdict_instruction({"verdict": "string"})
    assert "verdict" in schema_text and "JSON" in schema_text


def test_iter_and_last_json_object() -> None:
    text = 'a {"x": 1} b {"y": 2} c'
    objs = list(iter_json_objects(text))
    assert objs == [{"x": 1}, {"y": 2}]
    assert last_json_object(text) == {"y": 2}
    assert last_json_object("no json") is None


# --- aggregation -------------------------------------------------------------


def test_unanimous_all_agree() -> None:
    outcome, decision = aggregate(Strategy.UNANIMOUS, [_voice("yes"), _voice("yes")])
    assert outcome == "unanimous" and decision == "yes"


def test_unanimous_split_on_disagreement() -> None:
    outcome, _ = aggregate(Strategy.UNANIMOUS, [_voice("yes"), _voice("no")])
    assert outcome == "split"


def test_unanimous_failed_voice_vetoes() -> None:
    outcome, _ = aggregate(Strategy.UNANIMOUS, [_voice("yes"), _voice(None, ok=False)])
    assert outcome == "split"


def test_majority_true_majority_required() -> None:
    # 2 of 3 eligible -> majority; the failed third stays in the denominator
    outcome, decision = aggregate(Strategy.MAJORITY, [_voice("yes"), _voice("yes"), _voice(None, ok=False)])
    assert outcome == "majority" and decision == "yes"


def test_majority_no_majority_when_under_half() -> None:
    # 2 yes of 4 eligible is not > 50%
    outcome, _ = aggregate(Strategy.MAJORITY, [_voice("yes"), _voice("yes"), _voice("no"), _voice(None, ok=False)])
    assert outcome == "no_majority"


def test_plurality_top_scorer_even_below_half() -> None:
    outcome, decision = aggregate(Strategy.PLURALITY, [_voice("a"), _voice("a"), _voice("b"), _voice("c")])
    assert outcome == "plurality" and decision == "a"


def test_plurality_tie_at_top() -> None:
    outcome, _ = aggregate(Strategy.PLURALITY, [_voice("a"), _voice("b")])
    assert outcome == "tied"


def test_weighted_majority_by_weight() -> None:
    outcome, decision = aggregate(
        Strategy.WEIGHTED, [_voice("yes", weight=3.0), _voice("no", weight=1.0), _voice("no", weight=1.0)]
    )
    assert outcome == "majority" and decision == "yes"


def test_parity_pair_agree_and_escalate() -> None:
    proposer = _voice("ship", label="proposer", weight=2.0)
    counter_ok = _voice("ship", parity=True)
    assert aggregate(Strategy.PARITY_PAIR, [proposer, counter_ok]) == ("agree", "ship")
    counter_diff = _voice("hold", parity=True)
    assert aggregate(Strategy.PARITY_PAIR, [proposer, counter_diff])[0] == "escalate"
    # a failed counterweight cannot corroborate -> escalate
    assert aggregate(Strategy.PARITY_PAIR, [proposer, _voice(None, ok=False, parity=True)])[0] == "escalate"


def test_no_quorum_below_min_quorum() -> None:
    outcome, decision = aggregate(Strategy.MAJORITY, [_voice("yes")], min_quorum=2)
    assert outcome == "no_quorum" and decision is None


def test_all_voices_defensive_split() -> None:
    # ALL_VOICES does not aggregate; aggregate() returns a defensive split if ever reached
    assert aggregate(Strategy.ALL_VOICES, [_voice("yes")]) == ("split", None)


# --- stance ------------------------------------------------------------------


def test_apply_stance_wraps_for_and_against() -> None:
    assert apply_stance("P", Stance.FOR).startswith("Argue in favor")
    assert apply_stance("P", Stance.AGAINST).startswith("Argue against")
    assert apply_stance("P", Stance.NEUTRAL) == "P"
    assert apply_stance("P", None) == "P"


# --- diversity ---------------------------------------------------------------


def test_diversity_low_when_same_model() -> None:
    provs = [Provenance(provider="openai", model="gpt-x"), Provenance(provider="openai", model="gpt-x")]
    report = effective_diversity(provs, min_distinct=2)
    assert report.answered_voices == 2
    assert report.distinct_models == 1
    assert report.low_diversity is True


def test_diversity_high_when_distinct() -> None:
    provs = [Provenance(provider="openai", model="gpt-x"), Provenance(provider="anthropic", model="claude-y")]
    report = effective_diversity(provs, min_distinct=2)
    assert report.distinct_models == 2 and report.distinct_providers == 2
    assert report.low_diversity is False


def test_diversity_low_on_provider_axis_only() -> None:
    # two distinct model strings but the same vendor -> the provider axis flags it
    provs = [Provenance(provider="anthropic", model="opus"), Provenance(provider="anthropic", model="claude-opus-4")]
    report = effective_diversity(provs, min_distinct=2)
    assert report.distinct_models == 2 and report.distinct_providers == 1
    assert report.low_diversity is True


def test_diversity_unknown_excluded() -> None:
    provs = [Provenance(provider="openai", model="gpt-x"), None]
    report = effective_diversity(provs, min_distinct=2)
    assert report.unknown == 1 and report.distinct_models == 1
    # only one resolved model -> not flagged (an all-unknown or single-known panel is unmeasured)
    assert report.low_diversity is False
