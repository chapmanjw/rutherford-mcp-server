# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation depth guard and the target cap."""

from __future__ import annotations

import pytest

from rutherford.domain.errors import DepthLimitError, RutherfordError
from rutherford.runtime.depth import (
    ENV_DEPTH,
    child_depth_env,
    current_depth,
    ensure_within_depth,
    ensure_within_target_cap,
)


def test_current_depth_default_zero() -> None:
    assert current_depth(env={}) == 0


def test_current_depth_reads_env() -> None:
    assert current_depth(env={ENV_DEPTH: "2"}) == 2


def test_current_depth_invalid_is_zero() -> None:
    assert current_depth(env={ENV_DEPTH: "not-a-number"}) == 0
    assert current_depth(env={ENV_DEPTH: "-5"}) == 0


def test_child_depth_env_increments() -> None:
    assert child_depth_env(0) == {ENV_DEPTH: "1"}
    assert child_depth_env(2) == {ENV_DEPTH: "3"}


def test_ensure_within_depth_allows_below_max() -> None:
    ensure_within_depth(0, 3)
    ensure_within_depth(2, 3)


def test_ensure_within_depth_refuses_at_max() -> None:
    with pytest.raises(DepthLimitError) as info:
        ensure_within_depth(3, 3)
    assert info.value.details == {"depth": 3, "max_depth": 3}


def test_ensure_within_target_cap() -> None:
    ensure_within_target_cap(8, 8)
    with pytest.raises(RutherfordError, match="per-call cap"):
        ensure_within_target_cap(9, 8)
