# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for domain enums, error codes, and core model behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ALL_ERROR_CODES, ErrorCode, is_error_code
from rutherford.domain.errors import ConfigError, DepthLimitError, RutherfordError
from rutherford.domain.models import DelegationRequest, ErrorInfo, Target


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


def test_target_metadata_defaults_to_none() -> None:
    target = Target(cli="goose")
    assert target.role is None
    assert target.label is None
    assert target.weight is None
    assert target.parity is None
    assert target.stance is None


def test_target_display_label() -> None:
    assert Target(cli="a").display_label == "a"
    assert Target(cli="a", model="m").display_label == "a:m"
    assert Target(cli="a", model="m", label="primary").display_label == "primary"


def test_target_effective_weight_and_parity() -> None:
    assert Target(cli="a").effective_weight == 1.0
    assert Target(cli="a", weight=2.5).effective_weight == 2.5
    assert Target(cli="a").is_parity is False
    assert Target(cli="a", parity=True).is_parity is True


def test_target_rejects_a_negative_weight() -> None:
    # A negative weight would shrink the weighted-strategy denominator and fake a majority.
    with pytest.raises(ValidationError):
        Target(cli="a", weight=-1.0)
    assert Target(cli="a", weight=0.0).effective_weight == 0.0  # zero is allowed (no influence)


def test_target_with_metadata_is_still_frozen_and_hashable() -> None:
    from rutherford.domain.enums import Stance

    target = Target(cli="kiro", model="x", label="dissenter", weight=2.0, parity=True, stance=Stance.AGAINST)
    assert {target, target} == {target}  # hashable with metadata
    with pytest.raises(ValidationError):
        target.weight = 3.0  # type: ignore[misc]


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


def test_error_envelope_rejects_a_non_contract_code() -> None:
    # The error codes are a closed client contract; a typoed or ad-hoc code must fail at
    # construction -- both on the envelope model and at a RutherfordError raise site -- rather
    # than serialize cleanly into a client-visible result.
    with pytest.raises(ValidationError):
        ErrorInfo(code="INVALID_INPT", message="typo")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="INVALID_INPT"):
        RutherfordError("INVALID_INPT", "typo")
    # Valid code strings still coerce -- the ergonomic path is unchanged.
    assert ErrorInfo(code="TIMEOUT", message="slow").code is ErrorCode.TIMEOUT  # type: ignore[arg-type]
    assert RutherfordError("TIMEOUT", "slow").code is ErrorCode.TIMEOUT
