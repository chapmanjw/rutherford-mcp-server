# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Extraction of top-level JSON objects embedded in free text.

A model's answer often wraps a JSON object in prose, code fences, or several candidate objects.
:func:`iter_json_objects` yields each *top-level* object, and :func:`last_json_object` returns the
last one. The implementation is robust where a hand-rolled brace counter is not:

* Stray quotes, an apostrophe, or an unmatched ``{`` in the surrounding prose do not desynchronize
  it -- it scans from each ``{`` / ``[`` and lets :class:`json.JSONDecoder` parse a complete value
  there, skipping any start that does not begin valid JSON.
* It does not descend into arrays. A top-level array is parsed whole and skipped, so an object that
  is merely an *element* of a trailing array (``... {"verdict":"x"}\\n[{"file":"a"}]``) is not
  mistaken for a top-level object and cannot steal "last object" from the real one. A genuinely
  nested object inside an object's field value is still captured as part of its parent.

Pure and dependency-free, in the bottom layer so both the services (consensus strategies) and the
adapters can use it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

_DECODER = json.JSONDecoder()


def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Yield each top-level JSON object embedded in ``text``, in order.

    Scans from each ``{`` or ``[`` and uses :meth:`json.JSONDecoder.raw_decode` to parse a complete
    JSON value beginning there. A value that parses to an object is yielded; a value that parses to a
    top-level array (or any non-object) is consumed and skipped, so objects nested inside it do not
    leak out as if top-level. A start that does not begin valid JSON is skipped one character at a
    time, which tolerates stray quotes and unmatched braces in the prose around the object(s).
    """
    index = 0
    length = len(text)
    while index < length:
        start = _next_value_start(text, index)
        if start == -1:
            return
        try:
            value, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            yield value
        # ``end`` is the index just past the parsed value; advance at least one character so a
        # zero-width or malformed decode can never loop forever.
        index = max(end, start + 1)


def last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object in ``text`` that parses, or ``None``.

    "Last" so trailing output (a final summary object) wins over an earlier example object in the
    reasoning. See :func:`iter_json_objects` for the robustness guarantees, including that objects
    inside a top-level array are not treated as top-level.
    """
    found: dict[str, Any] | None = None
    for obj in iter_json_objects(text):
        found = obj
    return found


def _next_value_start(text: str, index: int) -> int:
    """Return the index of the next ``{`` or ``[`` at or after ``index``, or ``-1`` if neither.

    Both are candidate starts of a JSON value: seeking ``[`` too lets a top-level array be parsed
    (and skipped) as a unit, rather than the scanner stepping into it and yielding its elements.
    """
    brace = text.find("{", index)
    bracket = text.find("[", index)
    if brace == -1:
        return bracket
    if bracket == -1:
        return brace
    return min(brace, bracket)
