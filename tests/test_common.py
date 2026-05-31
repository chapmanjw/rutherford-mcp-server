# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the tool-layer input parsing helpers."""

from __future__ import annotations

import pytest

from rutherford.domain.enums import DelegationMode, SafetyMode, Stance
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import Target
from rutherford.tools.common import as_target, parse_mode, parse_safety_mode, parse_stances


def test_parse_safety_mode_valid_and_passthrough() -> None:
    assert parse_safety_mode("write") is SafetyMode.WRITE
    assert parse_safety_mode(SafetyMode.YOLO) is SafetyMode.YOLO


def test_parse_safety_mode_invalid() -> None:
    with pytest.raises(RutherfordError, match="safety_mode"):
        parse_safety_mode("nope")


def test_parse_mode() -> None:
    assert parse_mode("async") is DelegationMode.ASYNC
    assert parse_mode(DelegationMode.SYNC) is DelegationMode.SYNC
    with pytest.raises(RutherfordError, match="mode"):
        parse_mode("later")


def test_parse_stances() -> None:
    assert parse_stances(None) is None
    assert parse_stances(["for", "against", "neutral"]) == [Stance.FOR, Stance.AGAINST, Stance.NEUTRAL]
    assert parse_stances([Stance.FOR]) == [Stance.FOR]
    with pytest.raises(RutherfordError, match="stance"):
        parse_stances(["sideways"])


def test_as_target_variants() -> None:
    assert as_target(Target(cli="a", model="m")) == Target(cli="a", model="m")
    assert as_target({"cli": "a", "model": "m"}) == Target(cli="a", model="m")
    assert as_target({"cli": "a"}) == Target(cli="a", model=None)
    assert as_target("a") == Target(cli="a", model=None)
    assert as_target("a:opus") == Target(cli="a", model="opus")


def test_as_target_invalid() -> None:
    with pytest.raises(RutherfordError, match="cli"):
        as_target({"model": "m"})
    with pytest.raises(RutherfordError, match="cli"):
        as_target("")
    with pytest.raises(RutherfordError, match="interpret target"):
        as_target(123)  # type: ignore[arg-type]
