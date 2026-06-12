# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for building the registry from config (built-ins + generic adapters)."""

from __future__ import annotations

import pytest

from rutherford.adapters.registry import build_registry
from rutherford.config.schema import AdapterConfig, GenericAdapterConfig, RutherfordConfig
from rutherford.domain.errors import RegistryError

ALL_BUILTINS = [
    "antigravity",
    "claude_code",
    "codex",
    "copilot",
    "cursor",
    "droid",
    "goose",
    "kiro",
    "lmstudio",
    "ollama",
    "opencode",
    "qwen",
    "vibe",
]


def test_loads_all_builtins() -> None:
    assert build_registry(RutherfordConfig()).ids() == ALL_BUILTINS


def test_disabled_adapter_is_excluded() -> None:
    registry = build_registry(RutherfordConfig(adapters={"codex": AdapterConfig(enabled=False)}))
    assert not registry.has("codex")
    assert registry.has("claude_code")


def test_enabled_adapters_allowlist() -> None:
    registry = build_registry(RutherfordConfig(enabled_adapters=["claude_code", "codex"]))
    assert registry.ids() == ["claude_code", "codex"]


def test_unknown_enabled_adapter_raises() -> None:
    with pytest.raises(RegistryError, match="unknown adapter"):
        build_registry(RutherfordConfig(enabled_adapters=["ghost"]))


def test_generic_adapter_from_config() -> None:
    generic = GenericAdapterConfig(
        id="mycli", display_name="My CLI", binary="mycli", base_args=["run"], natively_read_only=True
    )
    registry = build_registry(RutherfordConfig(generic_adapters=[generic]))
    assert registry.has("mycli")
    assert registry.get("mycli").display_name == "My CLI"
    assert len(registry) == len(ALL_BUILTINS) + 1


def test_generic_adapter_in_allowlist() -> None:
    generic = GenericAdapterConfig(id="mycli", display_name="My CLI", binary="mycli", natively_read_only=True)
    registry = build_registry(RutherfordConfig(generic_adapters=[generic], enabled_adapters=["mycli"]))
    assert registry.ids() == ["mycli"]


def test_disabled_generic_adapter_is_excluded() -> None:
    # `[adapters.<id>] enabled = false` is documented to apply to generic adapters too -- a
    # disabled generic must not stay registered and callable.
    generic = GenericAdapterConfig(id="mycli", display_name="My CLI", binary="mycli", natively_read_only=True)
    registry = build_registry(
        RutherfordConfig(generic_adapters=[generic], adapters={"mycli": AdapterConfig(enabled=False)})
    )
    assert not registry.has("mycli")


def test_duplicate_generic_adapter_ids_fail_fast() -> None:
    # Two same-id generics would otherwise collapse last-wins before the registry's duplicate
    # check ran -- silently swapping a binary or safety fragment at startup.
    twins = [
        GenericAdapterConfig(id="mycli", display_name="First", binary="one", natively_read_only=True),
        GenericAdapterConfig(id="mycli", display_name="Second", binary="two", natively_read_only=True),
    ]
    with pytest.raises(RegistryError, match="duplicate generic adapter id"):
        build_registry(RutherfordConfig(generic_adapters=twins))


def test_generic_adapter_replaces_a_builtin_on_id_collision() -> None:
    # The documented override rule: a generic whose id collides with a built-in replaces the
    # built-in (it does not raise, and the built-in does not win).
    generic = GenericAdapterConfig(
        id="codex", display_name="My Codex Override", binary="mycodex", natively_read_only=True
    )
    registry = build_registry(RutherfordConfig(generic_adapters=[generic]))
    assert registry.get("codex").display_name == "My Codex Override"
    assert len(registry) == len(ALL_BUILTINS)  # replaced, not added
