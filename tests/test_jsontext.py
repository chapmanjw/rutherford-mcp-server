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


def test_truncated_trailing_array_does_not_steal_the_last_object() -> None:
    # The full-codebase panel's MAJOR: when the trailing array is CUT OFF (no closing bracket),
    # the scanner used to step one character in, re-find the inner object, and yield it as
    # top-level -- overriding the real verdict. Truncated array elements stay suppressed.
    text = '{"verdict": "block"}\nRelated: [{"verdict": "ignore"}'
    assert last_json_object(text) == {"verdict": "block"}


def test_truncated_array_with_several_elements_stays_suppressed() -> None:
    text = '{"verdict": "approve"}\nFiles: [{"file": "a.py"}, {"file": "b.py"},'
    assert last_json_object(text) == {"verdict": "approve"}


def test_a_real_object_after_a_prose_bracket_is_still_found() -> None:
    # A prose bracket is not JSON; the scanner must fall back to the one-character step and still
    # find the real object after it (the truncated-array suppression must not eat prose).
    text = 'see [the docs] for details\n{"verdict": "approve"}'
    assert last_json_object(text) == {"verdict": "approve"}


def test_truncated_array_before_a_later_real_object_does_not_hide_it() -> None:
    # Suppression consumes only the parseable elements; a well-formed object appearing later in
    # the text is still treated as top-level (the lenient reading -- it cannot be known whether
    # the unclosed array was meant to contain it).
    text = 'items: [{"file": "a.py"}, oops\n{"verdict": "approve"}'
    assert last_json_object(text) == {"verdict": "approve"}


def test_malformed_array_that_still_closes_stays_suppressed() -> None:
    # The closed-bracket branch of the truncated-array walk: the whole-array parse fails (a stray
    # double comma), but the element walk reaches the closing bracket -- everything inside stays
    # suppressed and the scan resumes past the bracket, exactly as for a well-formed array.
    text = '{"verdict": "block"} trailing: [{"verdict": "ignore"},,]'
    assert last_json_object(text) == {"verdict": "block"}
