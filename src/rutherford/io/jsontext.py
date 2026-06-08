# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Balanced-brace extraction of JSON objects embedded in free text.

A model's answer often wraps a JSON object in prose, code fences, or several candidate objects.
:func:`last_json_object` returns the last top-level ``{...}`` that parses as a JSON object, matching
braces correctly (it respects string literals and escapes), so a *nested* object is captured whole.
This replaces the naive non-greedy ``\\{.*?\\}`` regex that stopped at the first ``}`` and silently
dropped any verdict with an object-valued field. Pure and dependency-free, in the bottom layer so both
the services (consensus strategies) and the adapters can use it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def iter_json_objects(text: str) -> Iterator[str]:
    """Yield each balanced, top-level ``{...}`` span in ``text``, in order.

    Brace counting ignores ``{`` / ``}`` inside JSON string literals and honors backslash escapes.
    A span is not validated as parseable JSON here -- the caller runs :func:`json.loads` and skips
    spans that do not parse.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                yield text[start : index + 1]
                start = -1


def last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last top-level ``{...}`` in ``text`` that parses as a JSON object, or ``None``.

    "Last" so trailing output (a final summary object) wins over an earlier example object in the
    reasoning. Nested objects are matched whole, unlike a non-nesting regex.
    """
    found: dict[str, Any] | None = None
    for span in iter_json_objects(text):
        try:
            parsed = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found
