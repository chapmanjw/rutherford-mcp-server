# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the robust embedded-JSON object scanner (``io/jsontext``)."""

from __future__ import annotations

from rutherford.io.jsontext import iter_json_objects, last_json_object


def test_last_object_wins_over_an_earlier_one() -> None:
    assert last_json_object('{"a": 1}\n{"b": 2}') == {"b": 2}


def test_no_object_returns_none() -> None:
    assert last_json_object("just prose, no json here") is None


def test_nested_object_in_a_field_is_captured_whole() -> None:
    # An object-valued field must parse as part of its parent, not be split at the first '}'.
    obj = last_json_object('{"verdict": "block", "evidence": {"file": "x.py", "line": 12}}')
    assert obj == {"verdict": "block", "evidence": {"file": "x.py", "line": 12}}


def test_stray_quote_in_prose_does_not_hide_the_object() -> None:
    # A lone double-quote in the prose before the object must not desynchronize the scan.
    assert last_json_object('he said "go ahead\n{"verdict": "approve"}') == {"verdict": "approve"}


def test_unmatched_brace_in_prose_does_not_swallow_the_object() -> None:
    assert last_json_object('a broken { brace in prose\n{"verdict": "approve"}') == {"verdict": "approve"}


def test_object_inside_a_top_level_array_is_not_yielded() -> None:
    # Array elements are not top-level objects; the array is consumed whole and skipped.
    assert list(iter_json_objects('[{"a": 1}, {"b": 2}]')) == []
    assert last_json_object('[{"verdict": "a"}, {"verdict": "b"}]') is None


def test_trailing_array_does_not_steal_the_last_object() -> None:
    # The regression the bulletproofing audit caught: a real object followed by a trailing array of
    # objects (e.g. a "related files" list) must not override the real final object.
    text = '{"verdict": "block"}\nRelated: [{"file": "a.py"}, {"verdict": "ignore"}]'
    assert last_json_object(text) == {"verdict": "block"}


def test_consecutive_objects_yield_in_order() -> None:
    assert list(iter_json_objects('{"a": 1} then {"b": 2}')) == [{"a": 1}, {"b": 2}]


def test_unterminated_object_is_skipped() -> None:
    assert last_json_object('{"verdict": "x"') is None


def test_code_fenced_object_is_found() -> None:
    assert last_json_object('```json\n{"verdict": "approve"}\n```') == {"verdict": "approve"}
