# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for domain enums, error codes, and core model behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ALL_ERROR_CODES, ErrorCode, is_error_code
from rutherford.domain.errors import ConfigError, DepthLimitError, RutherfordError
from rutherford.domain.models import DelegationRequest, Target


def test_error_codes_are_strings_and_known() -> None:
    assert ErrorCode.BINARY_NOT_FOUND == "BINARY_NOT_FOUND"
    assert is_error_code("BINARY_NOT_FOUND")
    assert not is_error_code("NOT_A_REAL_CODE")
    assert "INTERNAL" in ALL_ERROR_CODES


def test_safety_mode_values() -> None:
    assert SafetyMode.READ_ONLY.value == "read_only"
    assert [m.value for m in SafetyMode] == ["read_only", "propose", "write", "yolo"]


def test_target_is_frozen_and_hashable() -> None:
    target = Target(cli="claude_code", model="opus")
    assert {target, Target(cli="claude_code", model="opus")} == {target}
    with pytest.raises(ValidationError):
        target.cli = "codex"  # type: ignore[misc]


def test_target_model_defaults_to_none() -> None:
    assert Target(cli="goose").model is None


def test_delegation_request_defaults_to_read_only_sync() -> None:
    req = DelegationRequest(target=Target(cli="codex"), prompt="hi")
    assert req.safety_mode is SafetyMode.READ_ONLY
    assert req.mode.value == "sync"
    assert req.depth == 0


def test_rutherford_error_carries_code() -> None:
    exc = RutherfordError(ErrorCode.TIMEOUT, "slow")
    assert exc.code == "TIMEOUT"
    assert exc.message == "slow"


def test_config_error_and_depth_error_have_codes() -> None:
    assert ConfigError("bad").code == ErrorCode.INVALID_INPUT
    assert DepthLimitError("deep").code == ErrorCode.MAX_DEPTH_EXCEEDED
