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


# --- F4a no-silent-dismissal (dissent stamping) ------------------------------


def test_stamp_dissent_marks_losing_verdicts_only() -> None:
    from rutherford.services.consensus import _stamp_dissent

    verdicts = [_voice("yes"), _voice("yes"), _voice("no"), _voice(None, ok=False)]
    _stamp_dissent(verdicts, "majority", "yes")
    assert verdicts[0].dissent is None and verdicts[1].dissent is None  # the winners are not "dissent"
    # the losing-but-parseable verdict carries a structural reason, distinct from no_verdict_reason
    assert verdicts[2].dissent == "minority: 1 of 3 voted 'no'; the panel majority 'yes'"
    assert verdicts[2].no_verdict_reason is None  # it HAD a verdict; it just lost
    # a failed voice has no verdict -> no dissent (no_verdict_reason carries its 'failed' instead)
    assert verdicts[3].dissent is None and verdicts[3].no_verdict_reason == "failed"


def test_stamp_dissent_noop_without_a_decision() -> None:
    from rutherford.services.consensus import _stamp_dissent

    verdicts = [_voice("a"), _voice("b")]  # a split -> no winner to dissent from
    _stamp_dissent(verdicts, "split", None)
    assert all(v.dissent is None for v in verdicts)


def test_stamp_dissent_is_honest_when_weight_overrides_head_count() -> None:
    from rutherford.services.consensus import _stamp_dissent

    # One heavy 'yes' outweighs two light 'no's: the HEAD count favors 'no' (2 of 3), but the panel decides
    # 'yes' on weight. The dissent must report each loser's OWN head count honestly, never the weighted total,
    # so a reader sees a weighted decision overrode a numeric majority rather than it being hidden.
    verdicts = [_voice("yes", weight=3.0), _voice("no", weight=1.0), _voice("no", weight=1.0)]
    outcome, decision = aggregate(Strategy.WEIGHTED, verdicts)
    assert (outcome, decision) == ("majority", "yes")
    _stamp_dissent(verdicts, outcome, decision)
    assert verdicts[0].dissent is None  # the weight-winner is not a dissent
    assert verdicts[1].dissent == "minority: 2 of 3 voted 'no'; the panel majority 'yes'"
    assert verdicts[2].dissent == verdicts[1].dissent  # both 'no' voters carry the same honest head count


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


def test_effective_lineages_is_the_vendor_proxy() -> None:
    # item 5 (5-A/5-B): effective_lineages = distinct vendor count now, as a NAMED headline concept that
    # the vote-math stretch can later rekey to model-family without changing the field.
    provs = [Provenance(provider="anthropic", model="opus"), Provenance(provider="anthropic", model="haiku")]
    report = effective_diversity(provs, min_distinct=2)
    assert report.effective_lineages == report.distinct_providers == 1


def test_diversity_headline_reads_as_one_sentence() -> None:
    # item 5 (5-C): a one-sentence trust summary -- effective lineages, low-diversity flag, unresolved note.
    # `headline` is a computed field (no parens), so it serializes onto the result wire, not just the methods.
    same = effective_diversity([Provenance(provider="openai", model="x"), Provenance(provider="openai", model="x")])
    assert same.headline == "1 effective lineage(s) among 2 answering voice(s); LOW DIVERSITY"
    diverse = effective_diversity(
        [Provenance(provider="openai", model="a"), Provenance(provider="anthropic", model="b")]
    )
    assert diverse.headline == "2 effective lineage(s) among 2 answering voice(s)"
    with_unknown = effective_diversity([Provenance(provider="openai", model="a"), None])
    assert "1 unresolved" in with_unknown.headline and "LOW DIVERSITY" not in with_unknown.headline


def test_diversity_headline_all_unknown_is_unmeasured_not_low() -> None:
    # 5-D conservative limit: all-unknown provenance -> 0 effective lineages, NOT a false LOW DIVERSITY flag.
    report = effective_diversity([None, None])
    assert report.effective_lineages == 0 and report.low_diversity is False
    assert report.headline == "0 effective lineage(s) among 2 answering voice(s), 2 unresolved"


def test_diversity_headline_single_voice_is_unmeasured() -> None:
    # A single resolved voice is unmeasured (no LOW DIVERSITY flag) -- the documented single-known-panel limit.
    report = effective_diversity([Provenance(provider="openai", model="a")])
    assert report.low_diversity is False
    assert report.headline == "1 effective lineage(s) among 1 answering voice(s)"


def test_diversity_headline_low_and_unresolved_together() -> None:
    # Both clauses co-occur and in order (unresolved note, THEN the low-diversity flag): two same-vendor
    # voices flag low diversity while a third voice's provenance is unresolved.
    report = effective_diversity(
        [Provenance(provider="openai", model="x"), Provenance(provider="openai", model="x"), None]
    )
    assert report.low_diversity is True and report.unknown == 1 and report.effective_lineages == 1
    assert report.headline == "1 effective lineage(s) among 3 answering voice(s), 1 unresolved; LOW DIVERSITY"


def test_diversity_zero_lineages_can_still_flag_on_the_model_axis() -> None:
    # The two signals are on separate axes: same model id with an UNRESOLVED vendor -> 0 measurable lineages
    # (the lineage/provider axis is unmeasured) yet the MODEL axis still catches the duplication. Honest, per
    # the headline docstring. `unknown` tracks the MODEL axis, so it is 0 here (the model resolved).
    report = effective_diversity([Provenance(model="dup"), Provenance(model="dup")])
    assert report.effective_lineages == 0 and report.low_diversity is True and report.unknown == 0
    assert report.headline == "0 effective lineage(s) among 2 answering voice(s); LOW DIVERSITY"


def test_diversity_headline_serializes_onto_the_result_wire() -> None:
    # Codex finding: a method is dropped by model_dump; a computed field is on the wire. Prove the sentence
    # is in the serialized payload (what an MCP client reads), not only callable in Python.
    from rutherford.io.serialize import to_plain

    report = effective_diversity([Provenance(provider="openai", model="x"), Provenance(provider="openai", model="x")])
    assert to_plain(report)["headline"] == "1 effective lineage(s) among 2 answering voice(s); LOW DIVERSITY"
