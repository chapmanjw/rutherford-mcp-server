# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The adapter registry: a closed mapping from id to adapter instance.

The registry is the composition point where concrete adapters are wired in -- the orchestration
core never imports an adapter, it asks the registry. The mapping is deliberately closed: looking
up an unknown id raises :class:`~rutherford.domain.errors.RegistryError` rather than silently
returning nothing, the way the owner's ``ToolCategory`` map raises on an unknown domain. Building
a registry rejects duplicate ids and unknown ids in ``enabled_adapters`` so misconfiguration
fails fast at startup.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable

from ..config.schema import RutherfordConfig
from ..domain.errors import RegistryError
from ..runtime.probe import CommandProbe
from .base import CLIAdapter

#: A factory takes an optional probe and returns a ready adapter instance.
BuiltinFactory = Callable[[CommandProbe | None], CLIAdapter]

#: The built-in adapter set, as ``(id, module, class)`` entries. Adding a code adapter is a
#: one-line addition here -- the registry imports the class by name, so it carries no
#: import-time dependency on any concrete adapter. Antigravity is last (its transcript quirk).
BUILTIN_ADAPTERS: tuple[tuple[str, str, str], ...] = (
    ("claude_code", "rutherford.adapters.claude_code", "ClaudeCodeAdapter"),
    ("codex", "rutherford.adapters.codex", "CodexAdapter"),
    ("opencode", "rutherford.adapters.opencode", "OpenCodeAdapter"),
    ("goose", "rutherford.adapters.goose", "GooseAdapter"),
    ("kiro", "rutherford.adapters.kiro", "KiroAdapter"),
    ("cursor", "rutherford.adapters.cursor", "CursorAdapter"),
    ("qwen", "rutherford.adapters.qwen", "QwenAdapter"),
    ("antigravity", "rutherford.adapters.antigravity", "AntigravityAdapter"),
)


class AdapterRegistry:
    """An immutable id -> adapter mapping with fail-fast lookup."""

    def __init__(self, adapters: Iterable[CLIAdapter]) -> None:
        mapping: dict[str, CLIAdapter] = {}
        for adapter in adapters:
            if adapter.id in mapping:
                raise RegistryError(f"duplicate adapter id {adapter.id!r}")
            mapping[adapter.id] = adapter
        self._adapters = mapping

    def get(self, cli_id: str) -> CLIAdapter:
        """Return the adapter for ``cli_id`` or raise :class:`RegistryError`."""
        try:
            return self._adapters[cli_id]
        except KeyError:
            known = ", ".join(self.ids()) or "(none)"
            raise RegistryError(f"unknown CLI id {cli_id!r}; known adapters: {known}") from None

    def has(self, cli_id: str) -> bool:
        """Return whether ``cli_id`` is registered."""
        return cli_id in self._adapters

    def ids(self) -> list[str]:
        """Return the registered adapter ids, sorted."""
        return sorted(self._adapters)

    def all(self) -> list[CLIAdapter]:
        """Return every registered adapter, ordered by id."""
        return [self._adapters[key] for key in self.ids()]

    def __contains__(self, cli_id: object) -> bool:
        return isinstance(cli_id, str) and cli_id in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)


def _load_factory(module_path: str, class_name: str) -> BuiltinFactory:
    """Import ``module_path`` and return its adapter class as a factory."""
    module = importlib.import_module(module_path)
    factory: BuiltinFactory = getattr(module, class_name)
    return factory


def build_registry(
    config: RutherfordConfig,
    *,
    probe: CommandProbe | None = None,
) -> AdapterRegistry:
    """Build the registry from config: enabled built-ins plus config-defined generic adapters.

    ``enabled_adapters``, when set, restricts the registry to those ids and raises if any names
    an adapter that is neither a built-in nor a configured generic adapter. A generic adapter
    whose id collides with a built-in replaces the built-in.

    Raises:
        RegistryError: On an unknown id in ``enabled_adapters`` or a duplicate adapter id.
    """
    adapters: dict[str, CLIAdapter] = {}

    for adapter_id, module_path, class_name in BUILTIN_ADAPTERS:
        entry = config.adapters.get(adapter_id)
        if entry is not None and not entry.enabled:
            continue
        adapters[adapter_id] = _load_factory(module_path, class_name)(probe)

    if config.generic_adapters:
        generic_module = importlib.import_module("rutherford.adapters.generic")
        generic_cls = generic_module.GenericAdapter
        for generic in config.generic_adapters:
            adapters[generic.id] = generic_cls(generic, probe=probe)

    if config.enabled_adapters is not None:
        allowed = set(config.enabled_adapters)
        unknown = allowed - set(adapters)
        if unknown:
            known = ", ".join(sorted(adapters)) or "(none)"
            raise RegistryError(
                f"enabled_adapters names unknown adapter(s): {', '.join(sorted(unknown))}; known adapters: {known}"
            )
        adapters = {key: value for key, value in adapters.items() if key in allowed}

    return AdapterRegistry(adapters.values())
