# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the TOON serialization seam and the result-envelope helpers."""

from __future__ import annotations

import pytest

from rutherford.context import error_payload_from, tool_error, tool_success
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationResult, Provenance, Target
from rutherford.io.serialize import DecodeError, decode, encode, to_plain


def test_encode_dict_is_toon() -> None:
    out = encode({"name": "Alice", "items": [1, 2, 3]})
    assert out == "name: Alice\nitems[3]: 1,2,3"


def test_encode_uniform_list_is_tabular() -> None:
    out = encode([{"cli": "claude_code", "ok": True}, {"cli": "codex", "ok": False}])
    # A uniform object array encodes as a single field header plus CSV-style rows.
    assert "{cli,ok}:" in out
    assert "claude_code,true" in out
    assert "codex,false" in out


def test_encode_none_is_null() -> None:
    assert encode(None) == "null"


def test_decode_round_trips_encode() -> None:
    data = {"panels": {"duo": {"strategy": "majority", "targets": [{"cli": "codex"}, {"cli": "kiro"}]}}}
    assert decode(encode(data)) == data


def test_decode_raises_decode_error_on_bad_toon() -> None:
    with pytest.raises(DecodeError):
        decode("items[3]: 1,2")  # declares three values but supplies two


def test_encode_empty_is_placeholder() -> None:
    assert encode({}) == "(no content)"


def test_to_plain_drops_none_and_serializes_enums() -> None:
    result = DelegationResult(
        target=Target(cli="claude_code", model="opus"),
        ok=True,
        text="done",
        safety_mode=SafetyMode.READ_ONLY,
    )
    plain = to_plain(result)
    assert plain["safety_mode"] == "read_only"
    assert plain["target"] == {"cli": "claude_code", "model": "opus"}
    # None fields (error, cost, session_id, raw) are dropped for compactness.
    assert "error" not in plain
    assert "cost" not in plain
    # A pre-F3 result carries no provenance, so the field is absent from the wire (backward-compat).
    assert "provenance" not in plain


def test_to_plain_includes_provenance_when_set() -> None:
    # A populated provenance block (F3) serializes; its own None sub-fields drop, confirmed stays.
    result = DelegationResult(
        target=Target(cli="claude_code", model="opus"),
        ok=True,
        text="done",
        provenance=Provenance(provider="anthropic", model="opus", cli_version="2.1.0", confirmed=True),
    )
    plain = to_plain(result)
    assert plain["provenance"] == {
        "provider": "anthropic",
        "model": "opus",
        "cli_version": "2.1.0",
        "confirmed": True,
    }
    assert "backend" not in plain["provenance"]  # unset sub-field dropped


def test_tool_success_encodes_model() -> None:
    result = DelegationResult(target=Target(cli="codex"), ok=True, text="hi")
    out = tool_success(result)
    assert "ok: true" in out
    assert "text: hi" in out


def test_tool_error_carries_code_and_message() -> None:
    out = tool_error(ErrorCode.BINARY_NOT_FOUND, "claude not found")
    assert "code: BINARY_NOT_FOUND" in out
    assert "message: claude not found" in out


def test_tool_error_includes_details_when_present() -> None:
    out = tool_error(ErrorCode.INVALID_INPUT, "bad", {"field": "prompt"})
    assert "details" in out
    assert "prompt" in out


def test_error_payload_from_exception() -> None:
    exc = RutherfordError(ErrorCode.MAX_DEPTH_EXCEEDED, "too deep", details={"depth": 3})
    out = error_payload_from(exc)
    assert "MAX_DEPTH_EXCEEDED" in out
    assert "too deep" in out
    assert "depth" in out
