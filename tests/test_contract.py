# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The shared adapter contract test, parametrized over every registered adapter.

Each adapter must satisfy the interface, build an argv list (never a shell string) from a pure
``build_invocation``, map every ``SafetyMode``, and return a normalized ``DelegationResult`` from
``parse_output`` -- including on non-zero exit and timeout.
"""

from __future__ import annotations

import pytest

from rutherford.adapters.base import CLIAdapter
from rutherford.adapters.registry import build_registry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    DelegationResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
    Target,
)

_REGISTRY = build_registry(RutherfordConfig())
_ADAPTERS = _REGISTRY.all()
_IDS = [adapter.id for adapter in _ADAPTERS]


def _ctx(adapter: CLIAdapter) -> InvocationContext:
    return InvocationContext(
        target=Target(cli=adapter.id, model="m"),
        safety_mode=SafetyMode.READ_ONLY,
        correlation_id="contract",
        working_dir="/tmp/work",
    )


def _req(adapter: CLIAdapter) -> DelegationRequest:
    return DelegationRequest(target=Target(cli=adapter.id, model="m"), prompt="ping", working_dir="/tmp/work")


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_satisfies_interface(adapter: CLIAdapter) -> None:
    assert isinstance(adapter, CLIAdapter)
    assert isinstance(adapter.id, str) and adapter.id
    assert isinstance(adapter.display_name, str) and adapter.display_name


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_build_invocation_is_an_argv_list_not_a_shell_string(adapter: CLIAdapter) -> None:
    spec = adapter.build_invocation(_req(adapter), _ctx(adapter))
    assert isinstance(spec, InvocationSpec)
    assert isinstance(spec.argv, list)
    assert spec.argv, "argv must not be empty"
    assert all(isinstance(arg, str) for arg in spec.argv)
    # The prompt is carried as its own argv element or on stdin -- never concatenated into a
    # single command string.
    assert "ping" in spec.argv or (spec.stdin is not None and "ping" in spec.stdin)


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_build_invocation_is_pure(adapter: CLIAdapter) -> None:
    first = adapter.build_invocation(_req(adapter), _ctx(adapter))
    second = adapter.build_invocation(_req(adapter), _ctx(adapter))
    assert first.argv == second.argv
    assert first.env == second.env
    assert first.stdin == second.stdin


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_map_safety_covers_every_mode(adapter: CLIAdapter) -> None:
    for mode in SafetyMode:
        flags = adapter.map_safety(mode)
        assert isinstance(flags, SafetyFlags)
        assert isinstance(flags.args, list)
        assert isinstance(flags.env, dict)


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_parse_output_returns_envelope(adapter: CLIAdapter) -> None:
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout="ping"), _ctx(adapter))
    assert isinstance(result, DelegationResult)
    assert result.target == _ctx(adapter).target


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_parse_output_nonzero_is_failure(adapter: CLIAdapter) -> None:
    result = adapter.parse_output(ProcessResult(exit_code=2, stdout="", stderr="boom"), _ctx(adapter))
    assert not result.ok
    assert result.error is not None


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_parse_output_timeout_is_timeout_error(adapter: CLIAdapter) -> None:
    result = adapter.parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx(adapter))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_registry_has_all_six_builtins() -> None:
    assert _IDS == ["antigravity", "claude_code", "codex", "goose", "kiro", "opencode"]
