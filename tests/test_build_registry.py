# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for building the registry from config (built-ins + generic adapters)."""

from __future__ import annotations

import pytest

from rutherford.adapters.registry import build_registry
from rutherford.config.schema import AdapterConfig, GenericAdapterConfig, RutherfordConfig
from rutherford.domain.errors import RegistryError

ALL_BUILTINS = ["antigravity", "claude_code", "codex", "cursor", "goose", "kiro", "ollama", "opencode", "qwen"]


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
    generic = GenericAdapterConfig(id="mycli", display_name="My CLI", binary="mycli", base_args=["run"])
    registry = build_registry(RutherfordConfig(generic_adapters=[generic]))
    assert registry.has("mycli")
    assert registry.get("mycli").display_name == "My CLI"
    assert len(registry) == len(ALL_BUILTINS) + 1


def test_generic_adapter_in_allowlist() -> None:
    generic = GenericAdapterConfig(id="mycli", display_name="My CLI", binary="mycli")
    registry = build_registry(RutherfordConfig(generic_adapters=[generic], enabled_adapters=["mycli"]))
    assert registry.ids() == ["mycli"]
