# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit tests for the lineage/depth guard (N1, item 3): the env helpers and the two cap checks."""

from __future__ import annotations

import pytest

from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.runtime.depth import (
    ENV_DEPTH,
    ENV_LINEAGE,
    ENV_PARENT_RUN,
    child_depth_env,
    child_env,
    child_lineage_env,
    current_depth,
    current_lineage_count,
    ensure_within_aggregate_cap,
    ensure_within_depth,
)


def test_current_depth_reads_and_defaults() -> None:
    assert current_depth({}) == 0  # unset -> 0
    assert current_depth({ENV_DEPTH: "3"}) == 3
    assert current_depth({ENV_DEPTH: "not-a-number"}) == 0  # invalid -> 0
    assert current_depth({ENV_DEPTH: "-5"}) == 0  # clamped to >= 0


def test_current_lineage_count_reads_and_defaults() -> None:
    assert current_lineage_count({}) == 0
    assert current_lineage_count({ENV_LINEAGE: "2"}) == 2
    assert current_lineage_count({ENV_LINEAGE: "bad"}) == 0


def test_child_depth_env_increments() -> None:
    assert child_depth_env(0) == {ENV_DEPTH: "1"}
    assert child_depth_env(4) == {ENV_DEPTH: "5"}


def test_child_lineage_env_count_first() -> None:
    # count-first: always sets the lineage count, includes the parent id only when known.
    assert child_lineage_env(current_count=0) == {ENV_LINEAGE: "1"}
    with_parent = child_lineage_env(parent_run_id="abc", current_count=2)
    assert with_parent == {ENV_LINEAGE: "3", ENV_PARENT_RUN: "abc"}


def test_child_env_combines_depth_and_lineage() -> None:
    env = child_env(1, parent_run_id="run-1", env={ENV_LINEAGE: "2"})
    assert env[ENV_DEPTH] == "2"  # base_depth 1 -> child 2
    assert env[ENV_LINEAGE] == "3"  # current count 2 -> child 3
    assert env[ENV_PARENT_RUN] == "run-1"


def test_ensure_within_depth_allows_below_ceiling() -> None:
    ensure_within_depth(0, 3)  # no raise
    ensure_within_depth(2, 3)  # last allowed depth


def test_ensure_within_depth_refuses_at_ceiling() -> None:
    with pytest.raises(RutherfordError) as exc:
        ensure_within_depth(3, 3)
    assert exc.value.code is ErrorCode.MAX_DEPTH_EXCEEDED
    assert exc.value.details == {"depth": 3, "max_depth": 3}


def test_aggregate_cap_disabled_is_within() -> None:
    assert ensure_within_aggregate_cap(99, None) is False  # no cap configured


def test_aggregate_cap_within_returns_false() -> None:
    assert ensure_within_aggregate_cap(2, 4) is False


def test_aggregate_cap_over_returns_true_advisory() -> None:
    assert ensure_within_aggregate_cap(5, 4) is True  # over the cap, advisory (no raise)


def test_aggregate_cap_over_enforced_raises() -> None:
    with pytest.raises(RutherfordError) as exc:
        ensure_within_aggregate_cap(5, 4, enforce=True)
    assert exc.value.code is ErrorCode.AGENT_CAP_EXCEEDED
    assert exc.value.details == {"declared": 5, "cap": 4}
