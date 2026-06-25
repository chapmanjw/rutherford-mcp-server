# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the ACP failure taxonomy (F7): unhealthy-vs-clean and model-unavailable classification."""

from __future__ import annotations

import pytest

from rutherford.acp.failures import indicates_unhealthy, is_model_unavailable
from rutherford.domain.error_codes import ErrorCode


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.ACP_SPAWN_FAILED,
        ErrorCode.ACP_HANDSHAKE_FAILED,
        ErrorCode.ACP_TURN_TIMEOUT,
        ErrorCode.ACP_TURN_ERROR,
        ErrorCode.RATE_LIMITED,
        ErrorCode.AUTH_FAILED,
    ],
)
def test_unhealthy_codes_count_toward_cooldown(code: ErrorCode) -> None:
    assert indicates_unhealthy(code) is True


@pytest.mark.parametrize(
    "code",
    [
        # A clean refusal or an empty answer is the REQUEST's fault, not the seat's, so it must not bench.
        ErrorCode.ACP_REFUSED,
        ErrorCode.ACP_EMPTY_ANSWER,
        # Guard / budget failures are not the seat being broken either.
        ErrorCode.UNKNOWN_TARGET,
        ErrorCode.WORKSPACE_NOT_TRUSTED,
        ErrorCode.BUDGET_EXHAUSTED,
        ErrorCode.INVALID_INPUT,
    ],
)
def test_clean_codes_do_not_count_toward_cooldown(code: ErrorCode) -> None:
    assert indicates_unhealthy(code) is False


@pytest.mark.parametrize(
    "message",
    [
        "Error: named models unavailable; switch to auto",
        "The requested model is not available on your plan",
        "unknown model: gpt-9",
        "Please UPGRADE your plan to continue",
        "model_unavailable",
        # The exact AWS Bedrock rejection a Claude Code seat hits when handed the bare cloud alias: the word
        # order ("model identifier is invalid") differs from the "invalid model" marker, so it needs its own.
        "ACP turn for claude_code failed: Internal error: API Error (claude-opus-4-8): 400 The provided model "
        "identifier is invalid.. Try --model to switch to us.anthropic.claude-opus-4-1-20250805-v1:0.",
    ],
)
def test_model_unavailable_messages_are_detected(message: str) -> None:
    assert is_model_unavailable(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "rate limit exceeded",
        "connection reset by peer",
        "the agent refused the request",
        "",
    ],
)
def test_non_model_unavailable_messages_are_not(message: str) -> None:
    assert is_model_unavailable(message) is False
