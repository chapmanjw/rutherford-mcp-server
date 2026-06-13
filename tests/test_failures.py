# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the F7 failure taxonomy: classification and the retryable / unhealthy policies."""

from __future__ import annotations

import pytest

from rutherford.domain.error_codes import ErrorCode
from rutherford.runtime.failures import (
    classify_failure,
    indicates_unhealthy,
    is_model_unavailable,
    is_retryable,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Error: 429 Too Many Requests", ErrorCode.RATE_LIMITED),
        ("the model is overloaded, please try again later", ErrorCode.RATE_LIMITED),
        ("RESOURCE_EXHAUSTED: quota exceeded", ErrorCode.RATE_LIMITED),
        ("401 Unauthorized", ErrorCode.AUTH_FAILED),
        ("authentication failed: invalid api key", ErrorCode.AUTH_FAILED),
        ("your token has expired, please log in", ErrorCode.AUTH_FAILED),
        ("This model's maximum context length is 200000 tokens", ErrorCode.CONTEXT_OVERFLOW),
        ("prompt is too long for the context window", ErrorCode.CONTEXT_OVERFLOW),
        ("named models unavailable on the free plan; switch to auto", ErrorCode.MODEL_UNAVAILABLE),
        ("unknown model 'gpt-9'", ErrorCode.MODEL_UNAVAILABLE),
        ("Segmentation fault (core dumped)", None),  # an opaque crash stays NONZERO_EXIT
        ("", None),
    ],
)
def test_classify_failure(message: str, expected: ErrorCode | None) -> None:
    assert classify_failure(message) == expected


def test_classify_failure_priority_rate_limit_before_auth() -> None:
    # A 429 that also says "unauthorized" is classified as the rate limit (checked first).
    assert classify_failure("429 too many requests (unauthorized retry)") is ErrorCode.RATE_LIMITED


def test_numeric_status_markers_match_on_a_word_boundary() -> None:
    # The HTTP statuses must not fire on an unrelated number that merely contains the digits.
    assert classify_failure("panic at line 4031, offset 14290") is None
    assert classify_failure("HTTP 403 Forbidden") is ErrorCode.AUTH_FAILED
    assert classify_failure("got a 429 back") is ErrorCode.RATE_LIMITED


def test_is_model_unavailable() -> None:
    assert is_model_unavailable("switch to auto") is True
    assert is_model_unavailable("model not available on your plan") is True
    assert is_model_unavailable("a normal failure") is False


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (ErrorCode.RATE_LIMITED, True),
        (ErrorCode.AUTH_FAILED, True),
        (ErrorCode.TIMEOUT, True),
        (ErrorCode.SPAWN_FAILED, True),
        (ErrorCode.BINARY_NOT_FOUND, True),
        (ErrorCode.MODEL_UNAVAILABLE, True),
        (ErrorCode.CONTEXT_OVERFLOW, True),
        (ErrorCode.NONZERO_EXIT, True),
        (ErrorCode.PARSE_ERROR, True),
        (ErrorCode.CONTRACT_MISMATCH, True),
        (ErrorCode.INVALID_INPUT, False),  # the request is bad; a different target won't help
        (ErrorCode.UNKNOWN_TARGET, False),
        (ErrorCode.WORKSPACE_NOT_TRUSTED, False),
        (ErrorCode.MAX_DEPTH_EXCEEDED, False),
        (ErrorCode.READONLY_VIOLATED, False),
        (ErrorCode.BUDGET_EXHAUSTED, False),  # F8a: a zero-yield harvest is a result, not a retry signal
    ],
)
def test_is_retryable(code: ErrorCode, retryable: bool) -> None:
    assert is_retryable(code) is retryable
    # Accepts the bare string too (the result envelope carries error.code as a str).
    assert is_retryable(str(code)) is retryable


@pytest.mark.parametrize(
    ("code", "unhealthy"),
    [
        (ErrorCode.RATE_LIMITED, True),
        (ErrorCode.AUTH_FAILED, True),
        (ErrorCode.TIMEOUT, True),
        (ErrorCode.SPAWN_FAILED, True),
        (ErrorCode.CONTRACT_MISMATCH, True),  # output drift is an adapter-integration problem
        (ErrorCode.NONZERO_EXIT, False),  # a healthy agent often exits non-zero on a hard task
        (ErrorCode.BINARY_NOT_FOUND, False),  # never reaches cooldown (returns at the install guard)
        (ErrorCode.CONTEXT_OVERFLOW, False),  # the prompt's fault, not the adapter's health
        (ErrorCode.MODEL_UNAVAILABLE, False),  # the model's fault
        (ErrorCode.PARSE_ERROR, False),  # likely a one-off mis-parse, not a down seat
        (ErrorCode.INVALID_INPUT, False),
        (ErrorCode.BUDGET_EXHAUSTED, False),  # F8a: the budget was too tight, not the adapter being down
    ],
)
def test_indicates_unhealthy(code: ErrorCode, unhealthy: bool) -> None:
    assert indicates_unhealthy(code) is unhealthy


def test_budget_exhausted_is_a_known_error_code() -> None:
    # F8a appended BUDGET_EXHAUSTED for the zero-yield harvest edge; it must be a real member with a
    # stable wire value, and (asserted above) neither retryable nor unhealthy.
    assert ErrorCode.BUDGET_EXHAUSTED.value == "BUDGET_EXHAUSTED"
