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
* That suppression survives a TRUNCATED array: when the value at a ``[`` fails to parse (the model
  cut off mid-array), the parseable elements are consumed and skipped rather than re-scanned, so
  ``... {"verdict":"x"}\\nRelated: [{"verdict":"ignore"}`` still returns the real verdict. A prose
  bracket whose contents are not JSON (``see [link]``) falls back to the one-character step.

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
        # RecursionError: a ~1000-deep bracket run blows the decoder's recursion before it can
        # report a decode error; the scanner must keep its never-raises posture and step past it.
        except (json.JSONDecodeError, RecursionError):
            if text[start] == "[":
                # A `[` that fails to parse whole may be a TRUNCATED array. Stepping one character
                # in would re-find its element objects and yield them as top-level -- letting a
                # cut-off trailing array steal "last object" from the real answer. Consume the
                # parseable elements instead, keeping them suppressed exactly as a well-formed
                # array's elements are.
                consumed = _skip_truncated_array_elements(text, start)
                if consumed > start:
                    index = consumed
                    continue
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


def _skip_truncated_array_elements(text: str, start: int) -> int:
    """Consume the parseable elements of a malformed/truncated array opening at ``start``.

    Returns the index just past the last consumed element (and past a closing ``]`` if one is
    reached), or ``start`` when the bracket is not followed by parseable JSON values -- a prose
    bracket like ``see [link]``, which the caller then steps past one character at a time exactly
    as before. Best-effort by design: a complete object nested inside a truncated *element* can
    still surface, but the common failure -- a cut-off trailing array of complete objects -- stays
    suppressed.
    """
    index = start + 1
    consumed = start
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break
        if text[index] == "]":
            return index + 1  # the array closed after all; everything inside stays suppressed
        try:
            _, end = _DECODER.raw_decode(text, index)
        except (json.JSONDecodeError, RecursionError):
            break  # the truncation point (or a too-deep element); stop consuming here
        index = end
        consumed = end
    return consumed if consumed > start else start
