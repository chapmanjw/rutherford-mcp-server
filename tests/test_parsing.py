# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit tests for the shared adapter parsing toolkit.

These exercise the utilities and parser strategies directly -- the audit edge cases each adapter
relies on (robust object scanning, the result:null != "None" rule, cost-key flexibility, the
answer/failure decision) -- so a regression surfaces here against the toolkit, not only
indirectly through one adapter's golden samples.
"""

from __future__ import annotations

from typing import Any

import pytest

from rutherford.adapters.parsing import (
    CostSpec,
    JsonEnvelopeParser,
    TextParser,
    as_text,
    dotted_get,
    extract_cost,
    finalize_answer,
    last_event,
    last_json_object,
    parse_json_array,
    parse_jsonl,
    render_terminal,
    str_field,
    strip_terminal_noise,
)
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import InvocationContext, ProcessResult, Target


def _ctx() -> InvocationContext:
    return InvocationContext(
        target=Target(cli="claude_code", model="opus"),
        safety_mode=SafetyMode.READ_ONLY,
        correlation_id="test",
    )


def _raw(stdout: str = "", *, exit_code: int | None = 0, stderr: str = "", timed_out: bool = False) -> ProcessResult:
    return ProcessResult(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out, duration_s=1.0)


# --- parse_jsonl -------------------------------------------------------------


def test_parse_jsonl_collects_objects_in_order() -> None:
    events = parse_jsonl('{"type":"a"}\n{"type":"b"}\n')
    assert [e["type"] for e in events] == ["a", "b"]


def test_parse_jsonl_skips_blank_and_non_object_lines() -> None:
    events = parse_jsonl('\n  \n[1, 2]\nhello\n{"type":"x"}\n')
    assert [e["type"] for e in events] == ["x"]


def test_parse_jsonl_skips_unparseable_lines() -> None:
    events = parse_jsonl('{"ok":true}\n{not valid json}\n{"also":1}\n')
    assert events == [{"ok": True}, {"also": 1}]


def test_parse_jsonl_empty_is_empty_list() -> None:
    assert parse_jsonl("") == []


# --- parse_json_array --------------------------------------------------------


def test_parse_json_array_returns_object_elements() -> None:
    events = parse_json_array('[{"type":"a"}, {"type":"b"}]')
    assert events == [{"type": "a"}, {"type": "b"}]


def test_parse_json_array_filters_non_object_elements() -> None:
    events = parse_json_array('[{"a":1}, 2, "x", [3], {"b":4}]')
    assert events == [{"a": 1}, {"b": 4}]


def test_parse_json_array_none_when_not_an_array() -> None:
    assert parse_json_array('{"type":"a"}') is None


def test_parse_json_array_none_when_not_json() -> None:
    assert parse_json_array("not json at all") is None


def test_parse_json_array_none_when_empty() -> None:
    assert parse_json_array("   ") is None


def test_parse_json_array_empty_array_is_empty_list_not_none() -> None:
    # An array that parses but holds nothing usable is [], distinct from None (not an array).
    assert parse_json_array("[]") == []


# --- last_event --------------------------------------------------------------


def test_last_event_returns_last_of_type() -> None:
    events: list[dict[str, Any]] = [{"type": "x", "n": 1}, {"type": "y"}, {"type": "x", "n": 2}]
    assert last_event(events, "x") == {"type": "x", "n": 2}


def test_last_event_none_when_absent() -> None:
    assert last_event([{"type": "x"}], "z") is None


# --- str_field ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"result": "hello"}, "hello"),
        ({"result": None}, ""),  # null result is "no answer", never the literal "None"
        ({"result": 42}, ""),  # a non-string result is not coerced
        ({}, ""),  # missing key
    ],
)
def test_str_field(payload: dict[str, object], expected: str) -> None:
    assert str_field(payload, "result") == expected


# --- as_text -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hi", "hi"),
        (42, "42"),
        (3.5, "3.5"),
        (True, None),  # a bool leaf is rejected (not a scalar answer)
        (None, None),
        ({"a": 1}, None),
        ([1, 2], None),
    ],
)
def test_as_text(value: object, expected: str | None) -> None:
    assert as_text(value) == expected


# --- dotted_get --------------------------------------------------------------


def test_dotted_get_follows_nested_path() -> None:
    assert dotted_get({"message": {"content": "hi"}}, "message.content") == "hi"


def test_dotted_get_none_on_missing_key() -> None:
    assert dotted_get({"message": {}}, "message.content") is None


def test_dotted_get_none_when_midpath_not_a_dict() -> None:
    assert dotted_get({"message": "flat"}, "message.content") is None


def test_dotted_get_single_key() -> None:
    assert dotted_get({"result": "x"}, "result") == "x"


# --- extract_cost / CostSpec -------------------------------------------------


def test_extract_cost_reads_tokens_directly_off_the_container() -> None:
    cost = extract_cost({"input_tokens": 10, "output_tokens": 3}, CostSpec())
    assert cost is not None
    assert (cost.input_tokens, cost.output_tokens) == (10, 3)


def test_extract_cost_reads_nested_tokens_and_usd() -> None:
    spec = CostSpec(usd_key="cost", tokens_key="tokens", input_keys=("input",), output_keys=("output",))
    cost = extract_cost({"cost": 0.02, "tokens": {"input": 5, "output": 7}}, spec)
    assert cost is not None
    assert cost.usd == 0.02
    assert (cost.input_tokens, cost.output_tokens) == (5, 7)


def test_extract_cost_top_level_usd_with_nested_token_block() -> None:
    spec = CostSpec(usd_key="total_cost_usd", tokens_key="usage")
    cost = extract_cost({"total_cost_usd": 0.5, "usage": {"input_tokens": 1, "output_tokens": 2}}, spec)
    assert cost is not None
    assert cost.usd == 0.5
    assert cost.total_tokens is None


def test_extract_cost_total_tokens() -> None:
    cost = extract_cost({"total_tokens": 99}, CostSpec(total_keys=("total_tokens",)))
    assert cost is not None
    assert cost.total_tokens == 99


def test_extract_cost_first_present_key_wins() -> None:
    cost = extract_cost({"prompt_tokens": 8}, CostSpec(input_keys=("input_tokens", "prompt_tokens")))
    assert cost is not None
    assert cost.input_tokens == 8


def test_extract_cost_none_when_no_figures() -> None:
    assert extract_cost({"unrelated": 1}, CostSpec()) is None


def test_extract_cost_none_when_container_not_a_dict() -> None:
    assert extract_cost(None, CostSpec()) is None
    assert extract_cost("nope", CostSpec()) is None


def test_extract_cost_missing_nested_block_is_none() -> None:
    # tokens_key points at a missing/non-dict block: no token figures, and no usd -> None.
    assert extract_cost({"usage": "oops"}, CostSpec(tokens_key="usage")) is None


def test_extract_cost_non_numeric_figure_degrades_to_none() -> None:
    # Raw CLI JSON flows into Cost: a non-numeric figure must yield cost=None instead of raising
    # ValidationError (which would sink a good answer as PARSE_ERROR). Numeric strings still coerce.
    assert extract_cost({"cost": "n/a"}, CostSpec(usd_key="cost")) is None
    coerced = extract_cost({"cost": "0.5"}, CostSpec(usd_key="cost"))
    assert coerced is not None
    assert coerced.usd == 0.5


# --- text cleaners -----------------------------------------------------------


def test_strip_terminal_noise_removes_ansi_and_trims() -> None:
    assert strip_terminal_noise("\x1b[32m  hello \x1b[0m\n") == "hello"


def test_render_terminal_collapses_carriage_return_overwrites() -> None:
    # A progress bar overwrites one line via \r; only what follows the last \r survives.
    rendered = render_terminal("loading 10%\rloading 50%\rdone\nanswer")
    assert rendered == "done\nanswer"


def test_render_terminal_normalizes_crlf_and_strips_ansi() -> None:
    assert render_terminal("\x1b[1mline1\x1b[0m\r\nline2") == "line1\nline2"


# --- last_json_object re-export ----------------------------------------------


def test_last_json_object_picks_last_object_around_prose() -> None:
    text = 'noise {"first":1} more prose {"verdict":"yes"}'
    assert last_json_object(text) == {"verdict": "yes"}


def test_last_json_object_skips_a_trailing_array() -> None:
    # A trailing top-level array must not steal "last object" from the real verdict object.
    text = '{"verdict":"x"}\n[{"file":"a"}]'
    assert last_json_object(text) == {"verdict": "x"}


def test_scanners_swallow_recursion_from_deep_bracket_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A deep-enough bracket run makes json's C parser raise RecursionError (not JSONDecodeError) before
    # any decode error is reported; the never-raises contracts must swallow it like any bad input. The
    # overflow depth is platform-dependent (a smaller C stack trips it on Windows where Linux/macOS
    # still parse, and json ignores sys.setrecursionlimit), so the raise is simulated at the decode
    # boundary rather than nested to an unportable depth. This pins all four widened except clauses:
    # parse_jsonl, parse_json_array, and both jsontext decode sites (the main scan and the
    # truncated-array skip reached from a leading "[").
    import json

    import rutherford.io.jsontext as jsontext_mod

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON value")

    # parse_jsonl / parse_json_array decode via json.loads; iter_json_objects via the shared decoder.
    monkeypatch.setattr(json, "loads", boom)
    monkeypatch.setattr(jsontext_mod._DECODER, "raw_decode", boom)

    assert parse_jsonl('{"a":1}\n{"ok":true}') == []  # every line raises -> skipped, never escapes
    assert parse_json_array('[{"verdict":"x"}]') is None
    assert last_json_object('{"verdict":"x"}') is None  # object start: the main jsontext decode site
    assert last_json_object('[{"verdict":"x"}]') is None  # array start: also the truncated-array skip


# --- JsonEnvelopeParser ------------------------------------------------------


def _envelope_parser() -> JsonEnvelopeParser:
    return JsonEnvelopeParser(
        cli_name="demo",
        is_error=lambda p: bool(p.get("is_error")),
        cost=CostSpec(usd_key="total_cost_usd", tokens_key="usage"),
        no_object_message="demo produced no parseable JSON object",
        no_text_message="demo reported success but had no result text",
    )


def test_envelope_success_extracts_text_session_cost() -> None:
    stdout = '{"result":"the answer","session_id":"s1","usage":{"input_tokens":4,"output_tokens":6}}'
    result = _envelope_parser().parse(_raw(stdout), _ctx())
    assert result.ok
    assert result.text == "the answer"
    assert result.session_id == "s1"
    assert result.cost is not None
    assert (result.cost.input_tokens, result.cost.output_tokens) == (4, 6)


def test_envelope_timeout() -> None:
    result = _envelope_parser().parse(_raw(timed_out=True, exit_code=None), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_envelope_no_object_clean_exit_is_parse_error() -> None:
    result = _envelope_parser().parse(_raw("not json", exit_code=0), _ctx())
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.error.message == "demo produced no parseable JSON object"


def test_envelope_no_object_nonzero_exit_surfaces_stderr() -> None:
    result = _envelope_parser().parse(_raw("not json", exit_code=1, stderr="boom"), _ctx())
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "boom" in result.error.message


def test_envelope_is_error_verdict_is_nonzero_exit() -> None:
    stdout = '{"result":"partial","is_error":true}'
    result = _envelope_parser().parse(_raw(stdout, exit_code=0), _ctx())
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert result.error.message == "partial"  # the in-band text is the error message


def test_envelope_nonzero_exit_uses_envelope_message_not_stderr() -> None:
    stdout = '{"result":"","subtype":"error_during_execution"}'
    result = _envelope_parser().parse(_raw(stdout, exit_code=2, stderr="ignored"), _ctx())
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert result.error.message == "error_during_execution"


def test_envelope_nonstring_subtype_falls_through_to_default_message() -> None:
    # The documented hardening over the originals: a non-string subtype is not coerced into the
    # error message; with no result text either, the message is the generic default.
    stdout = '{"result":"","subtype":{"unexpected":"shape"},"is_error":true}'
    result = _envelope_parser().parse(_raw(stdout, exit_code=0), _ctx())
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert result.error.message == "demo reported an error"


def test_envelope_null_result_is_parse_error_not_literal_none() -> None:
    # The audit fix: result:null on a clean exit is "no answer", never the literal "None".
    result = _envelope_parser().parse(_raw('{"result":null}', exit_code=0), _ctx())
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.error.message == "demo reported success but had no result text"


def test_envelope_whitespace_result_is_parse_error() -> None:
    result = _envelope_parser().parse(_raw('{"result":"   "}', exit_code=0), _ctx())
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_envelope_non_numeric_cost_degrades_to_none_and_keeps_the_answer() -> None:
    # A malformed cost figure in an otherwise good envelope must not sink the answer.
    stdout = '{"result":"the answer","total_cost_usd":"n/a","usage":{"input_tokens":"lots"}}'
    result = _envelope_parser().parse(_raw(stdout), _ctx())
    assert result.ok
    assert result.text == "the answer"
    assert result.cost is None


def test_envelope_without_is_error_relies_on_exit_code_only() -> None:
    # A CLI that signals failure only by exit code passes no is_error predicate.
    parser = JsonEnvelopeParser(
        cli_name="demo",
        cost=CostSpec(),
        no_object_message="no object",
        no_text_message="no text",
    )
    ok = parser.parse(_raw('{"result":"hi"}', exit_code=0), _ctx())
    assert ok.ok and ok.text == "hi"
    bad = parser.parse(_raw('{"result":"hi"}', exit_code=1, stderr="boom"), _ctx())
    assert bad.error is not None
    assert bad.error.code == "NONZERO_EXIT"


def test_envelope_contract_ok() -> None:
    parser = _envelope_parser()
    assert parser.contract_ok(_raw('{"result":"x"}')) is True
    assert parser.contract_ok(_raw("plain text")) is False


# --- TextParser --------------------------------------------------------------


def test_text_success_strips_ansi() -> None:
    result = TextParser().parse(_raw("\x1b[32mhello\x1b[0m\n"), _ctx())
    assert result.ok
    assert result.text == "hello"


def test_text_empty_is_parse_error_by_default() -> None:
    result = TextParser(empty_message="no output").parse(_raw(""), _ctx())
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.error.message == "no output"


def test_text_empty_is_success_when_allowed() -> None:
    result = TextParser(allow_empty=True).parse(_raw(""), _ctx())
    assert result.ok
    assert result.text == ""


def test_text_nonzero_surfaces_stderr_with_empty_body_by_default() -> None:
    result = TextParser().parse(_raw("partial output", exit_code=1, stderr="failed"), _ctx())
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert "failed" in result.error.message
    assert result.text == ""


def test_text_nonzero_surfaces_partial_output_when_configured() -> None:
    result = TextParser(surface_text_on_nonzero=True).parse(
        _raw("partial output", exit_code=1, stderr="failed"), _ctx()
    )
    assert result.error is not None
    assert result.text == "partial output"


def test_text_none_exit_is_treated_as_success() -> None:
    result = TextParser().parse(_raw("answer", exit_code=None), _ctx())
    assert result.ok
    assert result.text == "answer"


def test_text_validate_hook_fails_with_returned_message() -> None:
    def _reject_unterminated(text: str) -> str | None:
        return "truncated mid-reasoning" if "<think>" in text and "</think>" not in text else None

    parser = TextParser(validate=_reject_unterminated)
    result = parser.parse(_raw("<think>still going..."), _ctx())
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.error.message == "truncated mid-reasoning"


def test_text_validate_hook_passes_when_it_returns_none() -> None:
    parser = TextParser(validate=lambda _text: None)
    result = parser.parse(_raw("a fine answer"), _ctx())
    assert result.ok
    assert result.text == "a fine answer"


def test_text_custom_cleaner_is_applied() -> None:
    parser = TextParser(clean=lambda s: s.upper().strip())
    result = parser.parse(_raw("  hi  "), _ctx())
    assert result.ok
    assert result.text == "HI"


def test_text_timeout() -> None:
    result = TextParser().parse(_raw(timed_out=True, exit_code=None), _ctx())
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


# --- finalize_answer ---------------------------------------------------------


def test_finalize_clean_exit_with_answer_is_success() -> None:
    result = finalize_answer(_ctx(), _raw(exit_code=0), answer="hi", session_id="s", no_output_message="none")
    assert result.ok
    assert result.text == "hi"
    assert result.session_id == "s"


def test_finalize_clean_exit_empty_string_answer_counts_as_produced() -> None:
    # An empty-string answer (a completed-but-empty message) is still "produced", not a parse error.
    result = finalize_answer(_ctx(), _raw(exit_code=0), answer="", no_output_message="none")
    assert result.ok
    assert result.text == ""


def test_finalize_clean_exit_with_failure_is_nonzero_exit() -> None:
    result = finalize_answer(
        _ctx(), _raw(exit_code=0), answer="partial", failure="model blew up", no_output_message="none"
    )
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert result.error.message == "model blew up"
    assert result.text == "partial"


def test_finalize_clean_exit_no_answer_is_parse_error() -> None:
    result = finalize_answer(_ctx(), _raw("raw out", exit_code=0), answer=None, no_output_message="no agent message")
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert result.error.message == "no agent message"


def test_finalize_nonzero_exit_answer_wins_by_default() -> None:
    # A CLI can exit non-zero (e.g. a sandbox denial) yet have produced a valid answer.
    result = finalize_answer(_ctx(), _raw(exit_code=1), answer="valid answer", no_output_message="none")
    assert result.ok
    assert result.text == "valid answer"


def test_finalize_nonzero_exit_with_failure_overrides_an_interim_answer() -> None:
    # A failed run (turn.failed + non-zero exit) can still have produced interim narration that
    # parsed as an answer; the in-band failure wins, so the run never reads as success.
    result = finalize_answer(
        _ctx(),
        _raw(exit_code=1, stderr="boom"),
        answer="interim narration",
        failure="turn failed",
        no_output_message="none",
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert result.text == "turn failed"


def test_finalize_nonzero_exit_no_answer_surfaces_failure_body() -> None:
    result = finalize_answer(
        _ctx(), _raw(exit_code=1, stderr="boom"), answer=None, failure="turn failed", no_output_message="none"
    )
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"
    assert result.text == "turn failed"
