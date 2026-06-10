# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The failure taxonomy (F7): classify why a delegation failed, and decide what to do about it.

A churning roster means a panel seat is often dead, rate-limited, or auth-broken rather than wrong,
and a generic ``NONZERO_EXIT`` tells a caller (or the fallback logic) nothing actionable. This module
refines a failure into a specific, stable :class:`~rutherford.domain.error_codes.ErrorCode` by matching
the error text against marker tables -- the same heuristic shape the model-unavailable detection already
used -- and answers two policy questions the delegation service asks of a failure:

* :func:`is_retryable` -- could a *different* target plausibly succeed? (transient or target-specific
  failures -- a rate limit, a broken auth, this CLI not being installed -- vs. a request that is simply
  bad and would fail anywhere).
* :func:`indicates_unhealthy` -- does this failure suggest the *adapter seat itself* is broken, so it
  should count toward a cooldown? (a rate limit, a dead credential, or output drift -- not a bare
  non-zero exit, which a healthy agent often returns on a hard task, nor a context overflow that is
  the prompt's fault).

Pure and dependency-light, so the classification is unit-testable on its own.
"""

from __future__ import annotations

import re

from ..domain.error_codes import ErrorCode

#: Markers (matched case-insensitively as substrings) for a provider rate-limit / quota / overload --
#: a transient failure that a different target, or a later retry, may not hit. The HTTP status (429)
#: is matched separately on a word boundary so it does not fire on an unrelated number that merely
#: contains those digits (a line number, an offset).
_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "overloaded",
    "over capacity",
    "throttl",
    "try again later",
    "usage limit",
)

#: Markers for a runtime auth rejection (the CLI started but the provider refused the credential).
#: Deliberately specific -- ``permission denied`` / ``expired`` alone are too broad (file perms, a
#: session unrelated to auth) and would mis-route a generic failure. The HTTP statuses (401/403) are
#: matched separately on a word boundary.
_AUTH_MARKERS: tuple[str, ...] = (
    "unauthorized",
    "forbidden",
    "authentication failed",
    "authentication error",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "expired token",
    "token has expired",
    "not authenticated",
    "login required",
    "please log in",
    "please sign in",
    "invalid credentials",
)

#: Markers for the prompt-plus-context exceeding the model's window.
_CONTEXT_MARKERS: tuple[str, ...] = (
    "context length",
    "context window",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "token limit",
    "prompt is too long",
    "input is too long",
    "exceeds the maximum",
    "reduce the length",
    "maximum prompt length",
)

#: HTTP status codes matched on a word boundary (so ``403`` does not fire inside ``14034``).
_RATE_LIMIT_STATUS = re.compile(r"\b429\b")
_AUTH_STATUS = re.compile(r"\b(401|403)\b")

#: Markers a failure is "this model is not available to you" rather than a real error. Matched against
#: the error message; the cost of a false positive is one extra retry on the adapter's fallback model.
_MODEL_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "named models unavailable",
    "switch to auto",
    "only use auto",
    "model is not available",
    "model not available",
    "model unavailable",
    "model_unavailable",
    "no access to model",
    "not available on your plan",
    "upgrade your plan",
    "upgrade plans to continue",
    "unknown model",
    "invalid model",
)

#: Failure codes where trying a *different* target could plausibly succeed -- so a fallback chain is
#: worth attempting. Excludes request-is-bad codes (invalid input, unknown target, an untrusted
#: workspace, depth/target caps) that would fail the same way everywhere.
_RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.NONZERO_EXIT,
        ErrorCode.TIMEOUT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.AUTH_FAILED,
        ErrorCode.CONTEXT_OVERFLOW,
        ErrorCode.SPAWN_FAILED,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.BINARY_NOT_FOUND,
        ErrorCode.PARSE_ERROR,
        ErrorCode.CONTRACT_MISMATCH,
    }
)

#: Failure codes that suggest the adapter *seat* itself is broken (throttled, auth-dead, hung,
#: mis-launching, or its output drifted), so the failure should count toward its cooldown. Excludes a
#: bare ``NONZERO_EXIT`` -- a healthy agent often exits non-zero because the *task* was hard, and
#: benching a healthy adapter on task-shaped failures is the feature's most likely false positive --
#: along with the prompt's/model's fault (context overflow, a missing model) and a one-off mis-parse.
#: ``BINARY_NOT_FOUND`` is omitted because a not-installed adapter never reaches cooldown recording
#: (it returns at the install guard) and is already excluded from auto-panels.
_UNHEALTHY: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.TIMEOUT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.AUTH_FAILED,
        ErrorCode.SPAWN_FAILED,
        ErrorCode.CONTRACT_MISMATCH,
    }
)


def classify_failure(message: str) -> ErrorCode | None:
    """Refine a generic failure into a specific code by matching ``message``, or ``None`` if unknown.

    Returns the first of rate-limit / auth / context-overflow / model-unavailable whose markers appear
    in the message; otherwise ``None`` (leave the caller's existing code, e.g. ``NONZERO_EXIT``,
    unchanged). A heuristic over the error text: distinctive enough for the common provider errors,
    and bounded -- it only ever refines an already-failed result, never reclassifies a success.
    """
    lowered = message.lower()
    if _matches(lowered, _RATE_LIMIT_MARKERS) or _RATE_LIMIT_STATUS.search(lowered):
        return ErrorCode.RATE_LIMITED
    if _matches(lowered, _AUTH_MARKERS) or _AUTH_STATUS.search(lowered):
        return ErrorCode.AUTH_FAILED
    if _matches(lowered, _CONTEXT_MARKERS):
        return ErrorCode.CONTEXT_OVERFLOW
    if _matches(lowered, _MODEL_UNAVAILABLE_MARKERS):
        return ErrorCode.MODEL_UNAVAILABLE
    return None


def is_model_unavailable(message: str) -> bool:
    """Whether ``message`` looks like a model-availability rejection (drives same-adapter model fallback)."""
    return _matches(message.lower(), _MODEL_UNAVAILABLE_MARKERS)


def is_retryable(code: ErrorCode | str) -> bool:
    """Whether a failure with ``code`` is worth retrying on a *different* target."""
    return code in _RETRYABLE


def indicates_unhealthy(code: ErrorCode | str) -> bool:
    """Whether a failure with ``code`` should count toward the adapter's cooldown."""
    return code in _UNHEALTHY


def _matches(lowered: str, markers: tuple[str, ...]) -> bool:
    return any(marker in lowered for marker in markers)
