# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The shared adapter contract test, parametrized over every registered adapter.

Each adapter must satisfy the interface, build an argv list (never a shell string) from a pure
``build_invocation``, map every ``SafetyMode``, and return a normalized ``DelegationResult`` from
``parse_output`` -- including on non-zero exit and timeout.
"""

from __future__ import annotations

import pytest

from rutherford.adapters.base import BaseCLIAdapter, CLIAdapter
from rutherford.adapters.registry import build_registry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import (
    DelegationRequest,
    DelegationResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    Provenance,
    SafetyFlags,
    Target,
)
from tests.fakes import FakeProbe

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
    # `optional` is part of the contract (the runtime-checkable Protocol verifies presence, not type).
    assert isinstance(adapter.optional, bool)


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
def test_provenance_returns_a_block_and_never_raises(adapter: CLIAdapter) -> None:
    # F3: every adapter yields a Provenance whose provider/model are each present-as-string or a
    # graceful None, without raising -- the "present-or-unknown" contract. (An adapter with a fixed
    # model, e.g. Antigravity, legitimately reports its own model rather than the requested one.)
    prov = adapter.provenance(_ctx(adapter))
    assert isinstance(prov, Provenance)
    assert prov.provider is None or (isinstance(prov.provider, str) and prov.provider)
    assert prov.model is None or (isinstance(prov.model, str) and prov.model)


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_parse_output_timeout_is_timeout_error(adapter: CLIAdapter) -> None:
    result = adapter.parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx(adapter))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=_IDS)
def test_detect_reports_installed_and_absent(adapter: CLIAdapter) -> None:
    # Every registered adapter resolves its binary through the injected probe, so one which-hit /
    # which-miss pair covers detect() for the whole roster. Version *parsing* stays per-adapter
    # (ollama / lmstudio reshape their version output and keep their own golden tests).
    assert isinstance(adapter, BaseCLIAdapter)  # detect() and `binary` are BaseCLIAdapter scaffolding
    binary = adapter.binary
    probe = FakeProbe(
        which_map={binary: f"/usr/bin/{binary}"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout="1.0.0"),
    )
    installed = build_registry(RutherfordConfig(), probe=probe).get(adapter.id).detect()
    assert installed.installed
    assert installed.path == f"/usr/bin/{binary}"
    absent = build_registry(RutherfordConfig(), probe=FakeProbe(which_map={})).get(adapter.id).detect()
    assert not absent.installed


def test_registry_has_all_builtins() -> None:
    assert _IDS == [
        "amp",
        "antigravity",
        "claude_code",
        "cline",
        "cn",
        "codex",
        "copilot",
        "cursor",
        "droid",
        "goose",
        "hermes",
        "junie",
        "kilo",
        "kimi",
        "kiro",
        "lmstudio",
        "ollama",
        "opencode",
        "openhands",
        "pi",
        "qwen",
        "vibe",
    ]
