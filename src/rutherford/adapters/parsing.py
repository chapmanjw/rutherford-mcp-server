# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared parsing toolkit for adapter ``parse_output`` implementations.

Every adapter turns a CLI's raw stdout into the normalized :class:`DelegationResult`. Before
this module each adapter carried its own copy of the same plumbing -- a line-based JSON-object
scanner, a JSONL event splitter, a token-cost reader, a stdout cleaner -- and several copies had
quietly drifted apart (the envelope adapters never picked up the robust ``io.jsontext`` scanner;
the cost readers differed only in key names). This module factors that plumbing into three layers:

* **Pure utilities** (:func:`last_json_object`, :func:`parse_jsonl`, :func:`parse_json_array`,
  :func:`as_text`, :func:`str_field`, :func:`dotted_get`, :func:`extract_cost`, and the text
  cleaners) -- single-responsibility functions an adapter composes. These are where the real
  duplication lived, so this is where most of the saving is.
* **Parser strategies** (:class:`JsonEnvelopeParser`, :class:`TextParser`) -- small objects that
  capture an entire output *shape*. Two near-identical adapters (Claude Code + Cursor) and four
  text adapters (Goose, Kiro, Ollama, LM Studio) collapse to a few lines of configuration each,
  with their genuine differences expressed as constructor arguments, not forked code.
* **A shared finalizer** (:func:`finalize_answer`) for the event-stream adapters whose
  event-walking is genuinely bespoke (Codex, Qwen) but whose success/failure decision is not.

The intent is composition over inheritance: an adapter selects a strategy or assembles utilities,
rather than overriding hook methods on a base class. Adapters whose output is genuinely one of a
kind (Antigravity's transcript read, OpenCode's snapshot dedup) keep a hand-written ``parse_output``
and still draw on the utilities here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.error_codes import ErrorCode
from ..domain.models import Cost, DelegationResult, InvocationContext, ProcessResult
from ..io.jsontext import last_json_object
from .results import error_result, nonzero_result, strip_ansi, success_result, timeout_result

__all__ = [
    "CostSpec",
    "JsonEnvelopeParser",
    "TextParser",
    "as_text",
    "dotted_get",
    "extract_cost",
    "finalize_answer",
    "last_event",
    "last_json_object",
    "parse_json_array",
    "parse_jsonl",
    "render_terminal",
    "str_field",
    "strip_leading_reasoning",
    "strip_terminal_noise",
]


# --------------------------------------------------------------------------------------------------
# Pure utilities: structured-output readers
# --------------------------------------------------------------------------------------------------


def parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    """Return the JSON objects from a JSONL/NDJSON stream, skipping blank or unparseable lines.

    One object per line; a line that does not start with ``{`` or does not parse is ignored, so
    log noise interleaved with the event stream does not break parsing. Used by the line-delimited
    event adapters (Codex, OpenCode).

    Known, accepted tradeoff (panel-reviewed, settled MINOR): the leniency also swallows a
    *truncated* event line, so a partially corrupt stream whose earlier answer event parsed can
    still read as success. The agreed future remedy, if a real CLI ever emits that shape, is to
    split lenient log-scanning from a strict JSONL contract parse (returning invalid-line
    metadata for ``check_output_contract`` to reject) -- not to fail on every malformed ``{`` line
    here, which would break the documented log-noise tolerance.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def parse_json_array(stdout: str) -> list[dict[str, Any]] | None:
    """Return the object elements of a top-level JSON array, or ``None`` if stdout is not one.

    Distinct from :func:`parse_jsonl`: the whole stdout is a single JSON *array* of event objects
    (Qwen's ``-o json``), not one object per line. ``None`` distinguishes "not a JSON array at all"
    (a hard parse failure) from "an array with no usable elements" (an empty list).
    """
    candidate = stdout.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [event for event in parsed if isinstance(event, dict)]


def last_event(events: list[dict[str, Any]], etype: str) -> dict[str, Any] | None:
    """Return the last event whose ``type`` equals ``etype``, or ``None``."""
    for event in reversed(events):
        if event.get("type") == etype:
            return event
    return None


def str_field(payload: Mapping[str, Any], key: str) -> str:
    """Return ``payload[key]`` only when it is a string, else the empty string.

    The envelope CLIs (Claude Code, Cursor, Qwen) carry the answer in a single field that must be a
    string to count: a ``null`` or non-string ``result`` is treated as "no answer" (the empty
    string), never coerced to a literal ``"None"`` -- an output-drift fix preserved here in one place.
    """
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def as_text(value: Any) -> str | None:
    """Coerce a scalar leaf to text, or ``None`` for a container/boolean/missing value.

    Used by the generic adapter's dotted-path extraction: a string/number leaf becomes its text,
    while a ``dict``/``list``/``bool``/``None`` leaf returns ``None`` so a path that lands on a
    non-scalar is reported as a parse failure rather than stringified into ``"{...}"``/``"True"``.
    """
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    return str(value)


def dotted_get(payload: Mapping[str, Any], path: str) -> Any:
    """Follow a dotted key path (e.g. ``message.content``) into nested mappings, or ``None``."""
    current: Any = payload
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


# --------------------------------------------------------------------------------------------------
# Pure utilities: token-cost extraction
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostSpec:
    """Where to read token/cost figures from a CLI's JSON payload.

    The figures live in different places per CLI -- a top-level USD field here, token counts nested
    under ``usage`` there or carried inline in an event there -- but the *reading* is identical, so
    each adapter supplies a spec rather than its own ``_extract_cost``.

    * ``usd_key`` -- the key on the container holding the USD cost (``None`` if the CLI reports none).
    * ``tokens_key`` -- the key holding the nested token block; ``None`` means the container *is* the
      token block (token keys read directly off it).
    * ``input_keys`` / ``output_keys`` / ``total_keys`` -- candidate key names for each token count,
      tried in order; the first present, non-``None`` value wins.
    """

    usd_key: str | None = None
    tokens_key: str | None = None
    input_keys: tuple[str, ...] = ("input_tokens",)
    output_keys: tuple[str, ...] = ("output_tokens",)
    total_keys: tuple[str, ...] = ()


def extract_cost(container: Any, spec: CostSpec) -> Cost | None:
    """Build a :class:`Cost` from ``container`` per ``spec``, or ``None`` when no figure is present.

    Mirrors the previous per-adapter ``_extract_cost`` behavior: return ``None`` (not a zeroed
    :class:`Cost`) unless at least one of USD / input / output / total is present, so a result
    without cost data carries ``cost=None``.
    """
    if not isinstance(container, dict):
        return None
    usd = container.get(spec.usd_key) if spec.usd_key else None
    tokens: Any = container.get(spec.tokens_key) if spec.tokens_key else container
    if not isinstance(tokens, dict):
        tokens = {}
    input_tokens = _first_present(tokens, spec.input_keys)
    output_tokens = _first_present(tokens, spec.output_keys)
    total_tokens = _first_present(tokens, spec.total_keys)
    if usd is None and input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return Cost(usd=usd, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _first_present(tokens: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present, non-``None`` value among ``keys``, or ``None``."""
    for key in keys:
        value = tokens.get(key)
        if value is not None:
            return value
    return None


# --------------------------------------------------------------------------------------------------
# Pure utilities: text cleaning
# --------------------------------------------------------------------------------------------------


def strip_terminal_noise(stdout: str) -> str:
    """Strip ANSI escape sequences and surrounding whitespace -- the plain-text cleaner."""
    return strip_ansi(stdout).strip()


def render_terminal(stdout: str) -> str:
    """Render carriage-return overwrites the way a terminal would, after stripping ANSI.

    Some CLIs stream a load/progress bar to stdout as ``\\r`` overwrites on one line. Stripping ANSI,
    normalizing CRLF, then keeping only what survives the last bare ``\\r`` on each line drops the
    progress run and leaves the final rendered text. Not trimmed, so a caller can apply further
    cleaning before trimming.
    """
    text = strip_ansi(stdout).replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def strip_leading_reasoning(text: str, pattern: re.Pattern[str]) -> str:
    """Remove a single leading reasoning block matched by ``pattern`` (anchored at the start).

    ``pattern`` is expected to be anchored to ``\\A`` so only a *leading* block is removed; a
    matching tag that appears later in the answer (an example, a quoted tag) is left intact.
    """
    return pattern.sub("", text)


# --------------------------------------------------------------------------------------------------
# Parser strategies
# --------------------------------------------------------------------------------------------------


class JsonEnvelopeParser:
    """Parse a single-JSON-object envelope into the normalized result.

    Fits a CLI that, on ``--output-format json``, prints one object carrying the answer in a
    ``result`` field, a resumable ``session_id``, an ``is_error`` / ``subtype`` verdict, and a token
    ``usage`` block (Claude Code, Cursor). The genuine differences between such CLIs -- how an error
    is signalled, where the cost figures live, the CLI's name in messages -- are constructor
    arguments, so two adapters share one parser instead of two copies of the same control flow.

    The decision order follows the hand-written adapters: no parseable object -> non-zero exit
    becomes ``NONZERO_EXIT`` (surfacing stderr) else ``PARSE_ERROR``; an error verdict or a non-zero
    exit becomes ``NONZERO_EXIT`` carrying the envelope's own message; a success with no answer text
    becomes ``PARSE_ERROR``; otherwise success. (One deliberate hardening over the originals: a
    non-string ``subtype`` no longer reaches the error message -- it falls through to the default.)

    ``is_error`` is optional: a CLI that signals failure only by exit code passes nothing.
    ``result_key`` / ``subtype_key`` are flat top-level keys; a CLI that nests its answer (e.g. at
    ``message.content``) would extend this to read via :func:`dotted_get` + :func:`as_text` rather
    than drop to a hand-written parser.
    """

    def __init__(
        self,
        *,
        cli_name: str,
        cost: CostSpec,
        no_object_message: str,
        no_text_message: str,
        is_error: Callable[[Mapping[str, Any]], bool] | None = None,
        result_key: str = "result",
        session_key: str = "session_id",
        subtype_key: str = "subtype",
    ) -> None:
        self._cli = cli_name
        self._is_error = is_error
        self._cost = cost
        self._no_object_message = no_object_message
        self._no_text_message = no_text_message
        self._result_key = result_key
        self._session_key = session_key
        self._subtype_key = subtype_key

    def parse(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope. Never raises."""
        if raw.timed_out:
            return timeout_result(ctx, raw)

        payload = last_json_object(raw.stdout)
        if payload is None:
            if raw.exit_code != 0:
                return nonzero_result(ctx, raw)
            return error_result(ctx, raw, ErrorCode.PARSE_ERROR, self._no_object_message, text=raw.stdout.strip())

        text = str_field(payload, self._result_key)
        session_id = payload.get(self._session_key)
        if raw.exit_code != 0 or (self._is_error is not None and self._is_error(payload)):
            message = text or str_field(payload, self._subtype_key) or f"{self._cli} reported an error"
            return error_result(ctx, raw, ErrorCode.NONZERO_EXIT, message, text=text)
        if not text.strip():
            return error_result(ctx, raw, ErrorCode.PARSE_ERROR, self._no_text_message, text=raw.stdout.strip())

        return success_result(
            ctx,
            raw,
            text,
            session_id=str(session_id) if session_id else None,
            cost=extract_cost(payload, self._cost),
        )

    def contract_ok(self, raw: ProcessResult) -> bool:
        """The drift canary: a successful run must carry a parseable JSON object."""
        return last_json_object(raw.stdout) is not None


class TextParser:
    """Parse a plain-text answer into the normalized result.

    Fits a CLI whose answer is just text on stdout (Goose, Kiro, Ollama, LM Studio). The variation
    between them is captured by constructor arguments rather than four near-identical methods:

    * ``clean`` -- how to turn raw stdout into the answer (default: strip ANSI + trim). LM Studio
      passes a cleaner that also renders the load-progress bar and removes a leading think block.
    * ``allow_empty`` -- whether an empty answer on a clean exit is success (Goose, Kiro) or a
      ``PARSE_ERROR`` (Ollama, LM Studio, which expect a model to always produce text).
    * ``surface_text_on_nonzero`` -- whether a non-zero exit carries the cleaned partial output on
      the result (Kiro) or an empty body (the rest); stderr remains the error message either way.
    * ``validate`` -- an optional post-clean check (cleaned text -> error message or ``None``); a
      returned message fails the answer as a ``PARSE_ERROR``. This keeps any CLI-specific validation
      (e.g. LM Studio's "unterminated ``<think>`` block means truncated reasoning") in that adapter
      rather than as a flag baked into the shared parser.
    """

    def __init__(
        self,
        *,
        clean: Callable[[str], str] = strip_terminal_noise,
        allow_empty: bool = False,
        empty_message: str = "the CLI produced no output",
        surface_text_on_nonzero: bool = False,
        validate: Callable[[str], str | None] | None = None,
    ) -> None:
        self._clean = clean
        self._allow_empty = allow_empty
        self._empty_message = empty_message
        self._surface_text_on_nonzero = surface_text_on_nonzero
        self._validate = validate

    def parse(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope. Never raises."""
        if raw.timed_out:
            return timeout_result(ctx, raw)
        # A completed process always has an int exit code; ``None`` reaches here only from a path
        # that does not set ``timed_out`` (none in the real runner), and is treated as a clean exit.
        if raw.exit_code not in (0, None):
            text = self._clean(raw.stdout) if self._surface_text_on_nonzero else ""
            return nonzero_result(ctx, raw, text=text)

        text = self._clean(raw.stdout)
        if self._validate is not None:
            message = self._validate(text)
            if message is not None:
                return error_result(ctx, raw, ErrorCode.PARSE_ERROR, message)
        if not text:
            if self._allow_empty:
                return success_result(ctx, raw, text)
            return error_result(ctx, raw, ErrorCode.PARSE_ERROR, self._empty_message)
        return success_result(ctx, raw, text)


# --------------------------------------------------------------------------------------------------
# Shared finalizer for bespoke event-stream walkers
# --------------------------------------------------------------------------------------------------


def finalize_answer(
    ctx: InvocationContext,
    raw: ProcessResult,
    *,
    answer: str | None,
    no_output_message: str,
    session_id: str | None = None,
    cost: Cost | None = None,
    failure: str | None = None,
) -> DelegationResult:
    """Turn an extracted answer (plus optional session/cost/failure) into the normalized result.

    The event-stream adapters (Codex, Qwen) walk genuinely different event shapes, but once they
    have an ``answer`` and an optional in-band ``failure`` the success/failure decision is the same:

    * non-zero exit with an answer present -- the answer is still the result (a CLI can exit non-zero
      on a sandbox denial yet have produced a valid answer).
    * non-zero exit with no answer -- ``NONZERO_EXIT`` surfacing stderr (``failure`` as the body).
    * clean exit with an in-band ``failure`` -- ``NONZERO_EXIT`` carrying that message.
    * clean exit with no answer at all -- ``PARSE_ERROR`` (``no_output_message``).
    * otherwise -- success.

    ``answer`` is treated as present when it is not ``None`` (an empty-string answer still counts as
    produced), matching the hand-written walkers.
    """
    if raw.exit_code != 0:
        if answer is not None:
            return success_result(ctx, raw, answer, session_id=session_id, cost=cost)
        return nonzero_result(ctx, raw, text=failure or "")
    if failure is not None:
        return error_result(ctx, raw, ErrorCode.NONZERO_EXIT, failure, text=answer or "")
    if answer is None:
        return error_result(ctx, raw, ErrorCode.PARSE_ERROR, no_output_message, text=raw.stdout.strip())
    return success_result(ctx, raw, answer, session_id=session_id, cost=cost)
