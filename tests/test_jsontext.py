# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the embedded-JSON extractor (io/jsontext.py) -- the verdict/ranking parser's robustness core.

A model wraps its JSON in prose, code fences, example objects, or a trailing array. These pin the documented
guarantees: the LAST top-level object wins; a top-level array's element objects do NOT leak out as top-level
(even when the array is truncated); prose brackets/braces and stray quotes do not desynchronize the scan; and
the scanner never raises (a too-deep bracket run is stepped past, not propagated).
"""

from __future__ import annotations

from rutherford.io.jsontext import iter_json_objects, last_json_object


def test_extracts_an_object_wrapped_in_prose() -> None:
    assert last_json_object('The verdict is: {"verdict": "yes"} -- final.') == {"verdict": "yes"}


def test_last_object_wins_over_an_earlier_example() -> None:
    text = 'For example {"verdict": "no"} ... but my answer is {"verdict": "yes"}'
    assert last_json_object(text) == {"verdict": "yes"}


def test_iter_yields_every_top_level_object_in_order() -> None:
    text = '{"a": 1} noise {"b": 2} more {"c": 3}'
    assert list(iter_json_objects(text)) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_no_json_returns_none() -> None:
    assert last_json_object("just prose, no objects here") is None
    assert list(iter_json_objects("")) == []


def test_a_brace_that_does_not_begin_valid_json_is_skipped() -> None:
    # The first `{` opens nothing parseable; the scanner steps past it and finds the real object after.
    assert last_json_object('not json: {oops not json ... then {"verdict": "ok"}') == {"verdict": "ok"}


def test_stray_quotes_and_unmatched_braces_do_not_desync() -> None:
    text = 'it\'s a trap } with a stray " quote and { brace, answer: {"verdict": "yes"}'
    assert last_json_object(text) == {"verdict": "yes"}


def test_a_top_level_array_is_not_mistaken_for_its_element_objects() -> None:
    # The trailing array's element {"file": "a"} must NOT steal "last object" from the real verdict.
    text = '{"verdict": "x"}\n[{"file": "a"}, {"file": "b"}]'
    assert last_json_object(text) == {"verdict": "x"}


def test_a_truncated_trailing_array_still_yields_the_real_object() -> None:
    # The model cut off mid-array; its parseable elements are consumed/suppressed, not re-scanned as top-level.
    text = '{"verdict": "x"}\nRelated: [{"verdict": "ignore"}, {"verdict": "also-ignore"}'
    assert last_json_object(text) == {"verdict": "x"}


def test_a_prose_bracket_falls_back_to_a_character_step() -> None:
    # `see [link]` is not JSON; the scanner steps past it and still finds the object after.
    assert last_json_object('see [link] for context, then {"verdict": "yes"}') == {"verdict": "yes"}


def test_an_array_that_closes_cleanly_suppresses_all_its_objects() -> None:
    # Only the array (no real object) -> nothing leaks out as top-level.
    assert last_json_object('[{"file": "a"}, {"file": "b"}]') is None


def test_a_nested_object_in_a_field_is_captured_with_its_parent() -> None:
    obj = last_json_object('answer: {"verdict": "yes", "meta": {"score": 3}}')
    assert obj == {"verdict": "yes", "meta": {"score": 3}}


def test_a_pathologically_deep_bracket_run_does_not_raise() -> None:
    # ~1200 nested `[` blows the JSON decoder's recursion; the scanner must step past, never propagate, and
    # still return the real object that follows.
    text = "[" * 1200 + ' tail {"verdict": "yes"}'
    assert last_json_object(text) == {"verdict": "yes"}


def test_a_truncated_array_with_no_parseable_elements_is_stepped_past() -> None:
    # `[` followed by non-JSON prose: _skip_truncated_array_elements consumes nothing, the caller steps one
    # char, and the real object after is still found.
    assert last_json_object('[not json at all, then {"verdict": "yes"}') == {"verdict": "yes"}
