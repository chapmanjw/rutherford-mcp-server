# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the tool-layer input parsing helpers."""

from __future__ import annotations

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.domain.enums import DelegationMode, Effort, SafetyMode, Stance, Strategy
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import Target
from rutherford.tools.common import (
    as_target,
    ensure_known_cli,
    ensure_known_targets,
    parse_effort,
    parse_mode,
    parse_on_budget,
    parse_safety_mode,
    parse_stances,
    parse_strategy,
)
from tests.fakes import FakeAdapter


def test_ensure_known_cli_accepts_registered_and_rejects_unknown() -> None:
    registry = AdapterRegistry([FakeAdapter("a"), FakeAdapter("b")])
    ensure_known_cli(registry, "a")  # registered: no raise
    with pytest.raises(RutherfordError) as info:
        ensure_known_cli(registry, "ghost")
    assert info.value.code == "UNKNOWN_TARGET"


def test_ensure_known_targets_validates_every_target() -> None:
    registry = AdapterRegistry([FakeAdapter("a")])
    ensure_known_targets(registry, [Target(cli="a")])  # all known: no raise
    with pytest.raises(RutherfordError) as info:
        ensure_known_targets(registry, [Target(cli="a"), Target(cli="ghost")])
    assert info.value.code == "UNKNOWN_TARGET"


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


def test_parse_strategy() -> None:
    assert parse_strategy("parity-pair") is Strategy.PARITY_PAIR
    assert parse_strategy(Strategy.MAJORITY) is Strategy.MAJORITY
    assert parse_strategy("plurality") is Strategy.PLURALITY
    with pytest.raises(RutherfordError, match="strategy"):
        parse_strategy("bogus-strategy")


def test_parse_effort() -> None:
    # F8a: a valid tier coerces, an enum passes through, None (omitted) passes through for the service to
    # fill from default_effort, and an unknown tier is a clean tool-boundary error.
    assert parse_effort("high") is Effort.HIGH
    assert parse_effort(Effort.XHIGH) is Effort.XHIGH
    assert parse_effort(None) is None
    with pytest.raises(RutherfordError, match="effort"):
        parse_effort("turbo")


def test_parse_on_budget() -> None:
    # F8a 2-M: None (omitted) passes through so the service fills it from default_on_budget; the three
    # dispositions validate; anything else is a clean error.
    assert parse_on_budget(None) is None
    assert parse_on_budget("harvest") == "harvest"
    assert parse_on_budget("continue") == "continue"
    assert parse_on_budget("resume") == "resume"
    with pytest.raises(RutherfordError, match="on_budget"):
        parse_on_budget("abandon")


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


def test_as_target_full_metadata_dict() -> None:
    target = as_target(
        {
            "cli": "kiro",
            "model": "deepseek-3.2",
            "role": "dissenter",
            "label": "d",
            "weight": 2,  # int coerces to float
            "parity": True,
            "stance": "against",  # string coerces to the enum
        }
    )
    assert target == Target(
        cli="kiro",
        model="deepseek-3.2",
        role="dissenter",
        label="d",
        weight=2.0,
        parity=True,
        stance=Stance.AGAINST,
    )


def test_as_target_string_forms_carry_no_metadata() -> None:
    target = as_target("kiro:deepseek-3.2")
    assert (target.role, target.label, target.weight, target.parity, target.stance) == (None, None, None, None, None)
    assert target.display_label == "kiro:deepseek-3.2"


def test_as_target_label_defaulting() -> None:
    assert as_target("a").display_label == "a"
    assert as_target("a:m").display_label == "a:m"
    assert as_target({"cli": "a", "label": "primary"}).display_label == "primary"


def test_as_target_invalid_metadata_raises() -> None:
    with pytest.raises(RutherfordError, match="invalid target"):
        as_target({"cli": "a", "stance": "sideways"})
    with pytest.raises(RutherfordError, match="invalid target"):
        as_target({"cli": "a", "weight": "heavy"})
