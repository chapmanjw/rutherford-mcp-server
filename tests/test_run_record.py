# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the durable :class:`RunRecord` model (F2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rutherford.domain.enums import JobStatus, SafetyMode
from rutherford.domain.models import RunRecord


def _record(**kwargs: object) -> RunRecord:
    base: dict[str, object] = {"run_id": "r1", "kind": "delegate", "cli": "fake"}
    base.update(kwargs)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_run_record_is_frozen() -> None:
    # A run record is an immutable audit/replay entry, distinct from the mutable Job.
    record = _record()
    with pytest.raises(ValidationError):
        record.cli = "other"  # type: ignore[misc]


def test_run_record_defaults() -> None:
    record = _record()
    assert record.schema_version == 1
    assert record.status is JobStatus.SUCCEEDED
    assert record.ok is True
    assert record.argv == []
    assert record.changed_files == []
    assert record.safety_mode is SafetyMode.READ_ONLY
    assert record.parent_run_id is None


def test_env_is_not_a_field_so_secrets_never_reach_disk() -> None:
    # The child process env can carry API keys; it must never be persisted. Replay reconstructs it
    # from config instead. Guard the contract so a future field addition does not regress it.
    assert "env" not in RunRecord.model_fields


def test_none_fields_drop_from_the_persisted_shape() -> None:
    dumped = _record().model_dump(mode="json", exclude_none=True)
    assert "provenance" not in dumped
    assert "model" not in dumped
    assert "cost" not in dumped
    assert dumped["schema_version"] == 1
