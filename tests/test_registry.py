# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the adapter registry (closed mapping, fail-fast)."""

from __future__ import annotations

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.domain.errors import RegistryError
from tests.fakes import FakeAdapter


def test_registry_get_and_membership() -> None:
    registry = AdapterRegistry([FakeAdapter("a"), FakeAdapter("b")])
    assert registry.get("a").id == "a"
    assert registry.has("b")
    assert "a" in registry
    assert registry.ids() == ["a", "b"]
    assert [a.id for a in registry.all()] == ["a", "b"]
    assert len(registry) == 2


def test_registry_unknown_id_raises() -> None:
    registry = AdapterRegistry([FakeAdapter("a")])
    with pytest.raises(RegistryError) as info:
        registry.get("nope")
    assert "unknown CLI id" in str(info.value)
    assert "a" in str(info.value)


def test_registry_rejects_duplicate_ids() -> None:
    with pytest.raises(RegistryError, match="duplicate adapter id"):
        AdapterRegistry([FakeAdapter("dup"), FakeAdapter("dup")])
