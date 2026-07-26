# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the shared TOML basic-string quoter: everything it emits must parse back to the input."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from rutherford.io.tomltext import toml_str


def _round_trip(value: str) -> str:
    """Quote ``value`` as a TOML value, parse the fragment back, and return what tomllib read."""
    parsed = tomllib.loads(f"key = {toml_str(value)}")
    result = parsed["key"]
    assert isinstance(result, str)
    return result


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain",
        "C:\\Users\\me\\a workspace",  # Windows path: backslashes
        'he said "hi"',  # embedded quote
        'both \\ and " together',
        "tab\there",
        "newline\nhere",
        "carriage\rreturn",
        "form\ffeed",
        "back\bspace",
        "nul\x00byte",
        "bell\x07",
        "escape\x1b[0m",
        "delete\x7f",
        "caf\u00e9 \u2014 \U0001f680",  # non-ASCII passes through unescaped
    ],
)
def test_every_value_round_trips_through_tomllib(value: str) -> None:
    assert _round_trip(value) == value


def test_control_chars_without_a_named_escape_become_uxxxx() -> None:
    # \b \t \n \f \r have named escapes; everything else in the control range is \uXXXX.
    assert toml_str("\x01") == '"\\u0001"'
    assert toml_str("\x7f") == '"\\u007f"'
    assert toml_str("\t") == '"\\t"'


def test_ordinary_text_is_only_wrapped_in_quotes() -> None:
    assert toml_str("/home/me/ws") == '"/home/me/ws"'


@pytest.mark.parametrize("surrogate", ["\ud800", "\udcff", "\udfff"])
def test_a_lone_surrogate_is_refused(surrogate: str) -> None:
    """It must fail HERE, before the caller opens its destination file.

    A lone surrogate is what Python produces for a filename byte that is not valid UTF-8, so it reaches
    the quoter inside a path. Neither alternative to raising is acceptable, and both are asserted by the
    two tests below: escaping writes a file tomllib cannot load, and passing it through defers the error
    to a write that has already truncated its target.
    """
    with pytest.raises(ValueError, match="lone surrogate"):
        toml_str(f"ws{surrogate}x")


def test_escaping_a_surrogate_would_produce_a_file_tomllib_cannot_load() -> None:
    """Why toml_str does not simply emit \\uXXXX for a surrogate: the result is unloadable."""
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(r'key = "\ud800"')


def test_a_failed_utf8_write_truncates_its_target(tmp_path: Path) -> None:
    """Why toml_str does not pass a surrogate through: the caller's write destroys the file first.

    ``Path.write_text`` opens (truncating) and only then encodes, so deferring the failure to the write
    leaves a zero-byte file. Paired with setup's never-clobber guard that empty file is permanent, which
    is exactly the durable corruption the quoter exists to prevent.
    """
    target = tmp_path / "config.toml"
    target.write_text("existing = true\n", encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        target.write_text('k = "\udcff"\n', encoding="utf-8")
    assert target.exists() and target.stat().st_size == 0
