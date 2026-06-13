# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation depth guard and the target cap."""

from __future__ import annotations

import logging

import pytest

from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import DepthLimitError, RutherfordError
from rutherford.runtime.depth import (
    ENV_DEPTH,
    ENV_LINEAGE,
    ENV_PARENT_RUN,
    child_depth_env,
    child_lineage_env,
    current_depth,
    current_lineage_count,
    ensure_within_aggregate_cap,
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


# --- N1 (item 3): lineage env + advisory aggregate cap -----------------------


def test_current_lineage_count_default_zero() -> None:
    assert current_lineage_count(env={}) == 0


def test_current_lineage_count_reads_env() -> None:
    assert current_lineage_count(env={ENV_LINEAGE: "3"}) == 3


def test_current_lineage_count_invalid_is_zero() -> None:
    assert current_lineage_count(env={ENV_LINEAGE: "nope"}) == 0
    assert current_lineage_count(env={ENV_LINEAGE: "-4"}) == 0


def test_child_lineage_env_increments_count_only_by_default() -> None:
    # Count-first: the lineage count is always set; the parent run id only when one is known.
    assert child_lineage_env() == {ENV_LINEAGE: "1"}
    assert child_lineage_env(current_count=2) == {ENV_LINEAGE: "3"}
    assert ENV_PARENT_RUN not in child_lineage_env(current_count=2)


def test_child_lineage_env_carries_parent_run_when_given() -> None:
    assert child_lineage_env(parent_run_id="run-abc", current_count=1) == {
        ENV_LINEAGE: "2",
        ENV_PARENT_RUN: "run-abc",
    }


def test_ensure_within_aggregate_cap_no_cap_is_noop() -> None:
    ensure_within_aggregate_cap(1000, None)  # no cap configured -> never raises, never warns


def test_ensure_within_aggregate_cap_within_cap_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rutherford.runtime.depth"):
        ensure_within_aggregate_cap(8, 8)
    assert not caplog.records


def test_ensure_within_aggregate_cap_advisory_warns_not_raises(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rutherford.runtime.depth"):
        ensure_within_aggregate_cap(10, 8)  # over cap, but advisory (enforce=False) -> warn, do not raise
    assert any("aggregate-agent cap" in record.message for record in caplog.records)


def test_ensure_within_aggregate_cap_enforce_raises() -> None:
    with pytest.raises(RutherfordError) as info:
        ensure_within_aggregate_cap(10, 8, enforce=True)
    assert info.value.code == ErrorCode.AGENT_CAP_EXCEEDED
    assert info.value.details == {"declared": 10, "cap": 8}
