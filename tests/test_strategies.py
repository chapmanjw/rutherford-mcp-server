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
    extract_ranking,
    extract_verdict,
    lineage_discounts,
    rank_panel,
    ranking_instruction,
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


# --- F3 correlation-aware vote-math (opt-in lineage discount) ----------------


def _lv(verdict: str, provider: str | None, *, weight: float = 1.0) -> VoiceVerdict:
    return VoiceVerdict(
        label="v",
        cli="fake",
        weight=weight,
        ok=True,
        verdict=verdict,
        provenance=Provenance(provider=provider) if provider is not None else None,
    )


def test_lineage_discounts_collapse_a_shared_vendor_keep_others_full() -> None:
    voices = [_lv("yes", "alpha"), _lv("yes", "alpha"), _lv("no", "beta")]
    factors = lineage_discounts(voices)
    assert factors[id(voices[0])] == 0.5 and factors[id(voices[1])] == 0.5  # two-voice alpha lineage halved
    assert factors[id(voices[2])] == 1.0  # the lone beta keeps full weight


def test_lineage_discounts_treat_unknown_lineages_as_independent() -> None:
    voices = [_lv("yes", None), _lv("yes", None)]  # two UNRESOLVED-vendor voices
    factors = lineage_discounts(voices)
    assert factors[id(voices[0])] == 1.0 and factors[id(voices[1])] == 1.0  # NOT assumed correlated


def test_lineage_discounts_unknown_marker_cannot_collide_with_a_real_provider() -> None:
    # A provider literally named like the internal unknown-marker must NOT collapse with an unresolved voice.
    # Index 0 is the unresolved voice; the second voice's provider mimics a string sentinel for index 0.
    voices = [_lv("no", None), _lv("yes", "\x00unknown-0")]
    factors = lineage_discounts(voices)
    assert factors[id(voices[0])] == 1.0 and factors[id(voices[1])] == 1.0  # each its OWN lineage, never merged


def test_aggregate_discount_collapses_a_lineages_over_count() -> None:
    voices = [_lv("yes", "alpha"), _lv("yes", "alpha"), _lv("no", "beta")]
    assert aggregate(Strategy.MAJORITY, voices) == ("majority", "yes")  # off: 2 yes outvote 1 no
    # on: the two alpha votes are one effective vote (1.0) -> ties the independent beta no (1.0) -> no_majority
    assert aggregate(Strategy.MAJORITY, voices, correlation_discount=True) == ("no_majority", None)


def test_aggregate_discount_does_not_collapse_independent_lineages() -> None:
    voices = [_lv("yes", None), _lv("yes", None), _lv("no", "beta")]  # two distinct (unknown) lineages agree
    assert aggregate(Strategy.MAJORITY, voices, correlation_discount=True) == ("majority", "yes")


def test_aggregate_discount_applies_to_weighted() -> None:
    voices = [_lv("yes", "alpha", weight=2.0), _lv("yes", "alpha", weight=2.0), _lv("no", "beta", weight=3.0)]
    assert aggregate(Strategy.WEIGHTED, voices) == ("majority", "yes")  # off: weight 4 yes vs 3 no
    # on: alpha halved -> yes weight 2.0 vs no weight 3.0 -> the independent heavy 'no' now carries
    assert aggregate(Strategy.WEIGHTED, voices, correlation_discount=True) == ("majority", "no")


def test_aggregate_discount_off_is_byte_for_byte_the_old_behavior() -> None:
    voices = [_lv("yes", "alpha"), _lv("yes", "alpha"), _lv("no", "beta")]
    assert aggregate(Strategy.MAJORITY, voices) == aggregate(Strategy.MAJORITY, voices, correlation_discount=False)
    assert aggregate(Strategy.PLURALITY, voices) == aggregate(Strategy.PLURALITY, voices, correlation_discount=False)


# --- RANK: ballot extraction (F4b) -------------------------------------------


def test_ranking_instruction_line_and_json_shapes() -> None:
    line = ranking_instruction(["A", "B", "C"], None)
    assert "RANK:" in line and "A, B, C" in line
    js = ranking_instruction(["A", "B"], {"verdict": "string"})
    assert '"ranking"' in js and "JSON" in js


def test_extract_ranking_line_mode_normalizes_and_drops_unknown() -> None:
    text = "I reason about it.\nRANK: b, a, c, z"  # z is not a candidate label
    assert extract_ranking(text, None, ["A", "B", "C"]) == ["B", "A", "C"]  # upper-cased, z dropped


def test_extract_ranking_line_mode_last_wins_and_dedupes() -> None:
    text = "RANK: A, B\nthen on reflection\nRANK: C, A, A, B"  # last line wins; duplicate A collapses
    assert extract_ranking(text, None, ["A", "B", "C"]) == ["C", "A", "B"]


def test_extract_ranking_none_when_no_line() -> None:
    assert extract_ranking("just prose, no ranking", None, ["A", "B"]) is None


def test_extract_ranking_json_mode_last_object_with_a_list() -> None:
    text = 'draft {"ranking": ["A", "B"]} then final {"ranking": ["B", "A"]}\n{"tokens": 9}'
    assert extract_ranking(text, {"verdict": "string"}, ["A", "B"]) == ["B", "A"]


# --- RANK: Borda aggregation (F4b) -------------------------------------------


def _cands(*labels: str) -> list[tuple[str, str]]:
    return [(label, "fake") for label in labels]


def test_rank_panel_clear_winner_by_mean_rank() -> None:
    # Three self-excluded ballots that agree A > B > C: A is ranked best by both voters who can rank it.
    candidates = _cands("A", "B", "C")
    ballots = [("A", ["B", "C"]), ("B", ["A", "C"]), ("C", ["A", "B"])]
    outcome, decision, report = rank_panel(candidates, ballots)
    assert outcome == "ranked" and decision == "A" and report.winner == "A"
    leaderboard = {entry.label: entry for entry in report.leaderboard}
    assert [entry.label for entry in report.leaderboard] == ["A", "B", "C"]  # sorted best-first
    assert leaderboard["A"].rank == 1 and leaderboard["A"].mean_rank == 1.0 and leaderboard["A"].ballots == 2
    assert leaderboard["B"].mean_rank == 1.5 and leaderboard["C"].mean_rank == 2.0
    # Borda points: top of an L=2 ballot earns 2, bottom 1 -> A=4, B=3, C=2.
    assert leaderboard["A"].borda_points == 4.0 and leaderboard["C"].borda_points == 2.0
    assert report.ballots_cast == 3 and report.ballots_unparseable == 0


def test_rank_panel_three_candidates_have_no_pairwise() -> None:
    # With self-exclusion two voters share only the ONE candidate that is neither of them, so a 3-way panel
    # has no pair with >=2 common answers -> no pairwise rows, concordance undefined. (Pairwise needs N>=4.)
    _, _, report = rank_panel(_cands("A", "B", "C"), [("A", ["B", "C"]), ("B", ["A", "C"]), ("C", ["A", "B"])])
    assert report.pairwise == [] and report.concordance is None


def test_rank_panel_pairwise_and_concordance_when_voters_agree() -> None:
    # Four self-excluded ballots all agreeing A>B>C>D: every comparable voter pair correlates +1.
    candidates = _cands("A", "B", "C", "D")
    ballots = [("A", ["B", "C", "D"]), ("B", ["A", "C", "D"]), ("C", ["A", "B", "D"]), ("D", ["A", "B", "C"])]
    outcome, decision, report = rank_panel(candidates, ballots)
    assert outcome == "ranked" and decision == "A"
    assert all(pair.correlation == 1.0 for pair in report.pairwise) and report.concordance == 1.0
    assert report.pairwise  # N=4 -> each pair shares N-2 = 2 candidates, so pairs ARE defined


def test_rank_panel_total_disagreement_ties_with_negative_concordance() -> None:
    # Two voters who reverse each other over the SAME three answers: correlation -1, a three-way mean-rank tie.
    candidates = _cands("A", "B", "C", "D")
    outcome, decision, report = rank_panel(candidates, [("V1", ["A", "B", "C"]), ("V2", ["C", "B", "A"])])
    assert outcome == "tied" and decision is None and report.winner is None
    assert sorted(report.tied_top) == ["A", "B", "C"]  # all three share mean rank 2.0
    assert report.concordance == -1.0


def test_rank_panel_pairwise_uses_relative_order_not_absolute_position() -> None:
    # Two voters who AGREE on the order of their common answers (C < D < E) must correlate +1, even though one
    # interleaves non-common answers (A, B) that push C/D/E to different absolute positions. Absolute-position
    # Spearman would score this ~0.98; the dense re-rank of the common set restores the true 1.0.
    candidates = _cands("A", "B", "C", "D", "E")
    ballots = [("V1", ["A", "C", "B", "D", "E"]), ("V2", ["C", "D", "E"])]  # common {C,D,E}, same relative order
    _, _, report = rank_panel(candidates, ballots)
    pair = next(p for p in report.pairwise if {p.a, p.b} == {"V1", "V2"})
    assert pair.common == 3 and pair.correlation == 1.0


def test_rank_panel_below_quorum_is_no_quorum() -> None:
    outcome, decision, report = rank_panel(_cands("A", "B"), [("A", ["B"])], min_quorum=2)
    assert outcome == "no_quorum" and decision is None
    assert report.ballots_cast == 1 and report.leaderboard == []


def test_rank_panel_counts_an_unparseable_ballot() -> None:
    # A voter whose ballot referenced no known candidate (already filtered to []) is counted, not silent.
    candidates = _cands("A", "B", "C")
    ballots = [("A", ["B", "C"]), ("B", ["A", "C"]), ("C", [])]  # voter C cast nothing usable
    _, _, report = rank_panel(candidates, ballots)
    assert report.ballots_cast == 2 and report.ballots_unparseable == 1


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
