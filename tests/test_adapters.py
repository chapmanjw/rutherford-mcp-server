# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the npm ACP-adapter detection + opt-in install (codex-acp / claude-agent-acp / pi-acp).

When an agent's launch command is a SEPARATE npm adapter shim fronting an underlying CLI, and the CLI is
present but the shim is not, Rutherford recognizes the installable gap: ``doctor`` reports it with the exact
``npm i -g`` instruction (instead of a flat ``not_installed``), and ``setup install_adapters=true`` installs
it. These pin the state machine, the install action, the conformance enrichment, and the setup wiring.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from rutherford.acp.adapters import (
    AdapterInstall,
    AdapterState,
    adapter_install_command,
    adapter_state,
    install_adapter,
    install_hint,
    installable_adapters,
)
from rutherford.acp.conformance import _connect_failure, probe_agent
from rutherford.acp.descriptors import HIGH_FIDELITY, AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import build_app_context
from rutherford.io.serialize import decode
from rutherford.tools.setup import setup_tool

_BY_ID = {d.id: d for d in HIGH_FIDELITY}
CODEX = _BY_ID["codex"]
CLAUDE = _BY_ID["claude_code"]
PI = _BY_ID["pi"]
GOOSE = _BY_ID["goose"]


def _which(present: set[str]) -> Callable[[str], str | None]:
    """A fake ``shutil.which``: resolve the names in ``present``, miss everything else."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- the descriptor metadata -------------------------------------------------


def test_wrapped_adapters_carry_install_metadata() -> None:
    assert CODEX.is_wrapped_adapter and CODEX.underlying_cli == "codex"
    assert CODEX.adapter_package == "@agentclientprotocol/codex-acp"
    assert CLAUDE.underlying_cli == "claude" and CLAUDE.adapter_package == "@agentclientprotocol/claude-agent-acp"
    assert PI.underlying_cli == "pi" and PI.adapter_package == "pi-acp"
    # An agent that IS its own ACP server has no separate shim to set up.
    assert not GOOSE.is_wrapped_adapter and GOOSE.underlying_cli is None


# --- the install-state machine -----------------------------------------------


def test_adapter_state_not_applicable_for_a_self_hosted_agent() -> None:
    assert adapter_state(GOOSE, which=_which({"goose"})) is AdapterState.NOT_APPLICABLE


def test_adapter_state_installed_when_the_shim_is_on_path() -> None:
    assert adapter_state(CODEX, which=_which({"codex-acp", "codex"})) is AdapterState.INSTALLED


def test_adapter_state_cli_present_when_only_the_underlying_cli_is_there() -> None:
    # The exact case from the bug report: `codex` installed, `codex-acp` adapter not.
    assert adapter_state(CODEX, which=_which({"codex"})) is AdapterState.CLI_PRESENT


def test_adapter_state_cli_absent_when_neither_is_present() -> None:
    assert adapter_state(CODEX, which=_which(set())) is AdapterState.CLI_ABSENT


# --- the install hint (what doctor shows) ------------------------------------


def test_install_hint_only_when_cli_present_and_shim_missing() -> None:
    hint = install_hint(CODEX, which=_which({"codex"}))
    assert hint is not None and "npm i -g @agentclientprotocol/codex-acp" in hint and "codex-acp" in hint
    assert install_hint(CODEX, which=_which({"codex-acp", "codex"})) is None  # already installed
    assert install_hint(CODEX, which=_which(set())) is None  # CLI absent -> install the CLI, not the shim
    assert install_hint(GOOSE, which=_which({"goose"})) is None  # not a wrapped adapter


def test_adapter_install_command_is_the_npm_argv() -> None:
    assert adapter_install_command(CODEX) == ("npm", "i", "-g", "@agentclientprotocol/codex-acp")
    assert adapter_install_command(GOOSE) is None


def test_installable_adapters_filters_the_registry() -> None:
    registry = DescriptorRegistry([CODEX, CLAUDE, PI, GOOSE])
    # codex + pi CLIs present (shims absent); claude CLI absent; goose self-hosted.
    out = installable_adapters(registry, which=_which({"codex", "pi"}))
    assert {d.id for d in out} == {"codex", "pi"}


# --- the install action ------------------------------------------------------


def test_install_adapter_runs_npm_with_the_curated_package() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(cmd: tuple[str, ...]) -> tuple[bool, str]:
        calls.append(cmd)
        return True, "installed @agentclientprotocol/codex-acp"

    out = install_adapter(CODEX, which=_which({"codex", "npm"}), runner=runner)
    assert out.ok and out.agent_id == "codex" and out.package == "@agentclientprotocol/codex-acp"
    assert calls == [("npm", "i", "-g", "@agentclientprotocol/codex-acp")]  # argv from the curated constant only


def test_install_adapter_refuses_cleanly_without_npm() -> None:
    out = install_adapter(CODEX, which=_which({"codex"}), runner=lambda _c: (True, "x"))  # no npm on PATH
    assert not out.ok and "npm" in out.detail


def test_install_adapter_refuses_a_non_wrapper_agent() -> None:
    out = install_adapter(GOOSE, which=_which({"goose", "npm"}), runner=lambda _c: (True, "x"))
    assert not out.ok and "no separate npm adapter" in out.detail


def test_install_adapter_surfaces_an_npm_failure() -> None:
    out = install_adapter(PI, which=_which({"pi", "npm"}), runner=lambda _c: (False, "npm exited 1: EACCES"))
    assert not out.ok and "EACCES" in out.detail


def test_run_npm_classifies_success_failure_and_a_dead_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    from rutherford.acp.adapters import _run_npm

    class _Done:
        def __init__(self, rc: int, out: str = "", err: str = "") -> None:
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr("rutherford.acp.adapters.subprocess.run", lambda *_a, **_k: _Done(0))
    assert _run_npm(("npm", "i", "-g", "pkg")) == (True, "installed pkg")

    failed = _Done(1, err="line1\nEACCES denied")
    monkeypatch.setattr("rutherford.acp.adapters.subprocess.run", lambda *_a, **_k: failed)
    ok, detail = _run_npm(("npm", "i", "-g", "pkg"))
    assert ok is False and "EACCES denied" in detail  # the last stderr line is surfaced

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("npm vanished")

    monkeypatch.setattr("rutherford.acp.adapters.subprocess.run", _boom)
    ok2, detail2 = _run_npm(("npm", "i", "-g", "pkg"))
    assert ok2 is False and "failed" in detail2


# --- doctor / conformance enrichment -----------------------------------------


def test_connect_failure_adds_the_install_hint_only_for_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rutherford.acp.conformance.adapter_install_hint", lambda d: f"HINT:{d.id}")
    not_installed = _connect_failure(CODEX, "not_installed", False, "spawn failed", 0.0)
    assert not_installed.install_hint == "HINT:codex" and "HINT:codex" in not_installed.detail
    # A handshake_failed (the binary DID spawn) is a different problem -- no install hint.
    handshake = _connect_failure(CODEX, "handshake_failed", True, "bad handshake", 0.0)
    assert handshake.install_hint is None and "HINT" not in handshake.detail


async def test_probe_enriches_a_not_installed_wrapped_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wrapped-adapter descriptor whose shim binary does not exist spawns-fails -> not_installed, then the
    # probe enriches it with the install hint (computed against the real machine via adapter_install_hint,
    # stubbed here so the test does not depend on whether codex is installed on the runner).
    monkeypatch.setattr("rutherford.acp.conformance.adapter_install_hint", lambda d: "INSTALL-IT")
    descriptor = AgentDescriptor(
        "codex", "Codex", ("rutherford-no-such-acp-shim",), underlying_cli="codex", adapter_package="@x/codex-acp"
    )
    report = await probe_agent(descriptor, timeout_s=10.0)
    assert report.status == "not_installed" and report.install_hint == "INSTALL-IT" and "INSTALL-IT" in report.detail


# --- setup wiring ------------------------------------------------------------


async def test_setup_reports_installable_adapters_without_installing(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([CODEX, GOOSE]))
    monkeypatch.setattr("rutherford.tools.setup.installable_adapters", lambda _reg: [CODEX])
    monkeypatch.setattr(
        "rutherford.tools.setup.install_adapter",
        lambda _d: pytest_fail_if_called(),  # must NOT install
    )
    payload = decode(await setup_tool(app, install_adapters=False))
    adapters = payload["adapters"]
    assert adapters["installable"] == [
        {
            "agent": "codex",
            "package": "@agentclientprotocol/codex-acp",
            "command": "npm i -g @agentclientprotocol/codex-acp",
        }
    ]
    assert "installed" not in adapters  # report-only, nothing ran


async def test_setup_installs_adapters_on_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([CODEX, GOOSE]))
    installed_ids: list[str] = []

    def fake_install(descriptor: AgentDescriptor) -> AdapterInstall:
        installed_ids.append(descriptor.id)
        return AdapterInstall(
            descriptor.id, descriptor.adapter_package or "", True, f"installed {descriptor.adapter_package}"
        )

    monkeypatch.setattr("rutherford.tools.setup.installable_adapters", lambda _reg: [CODEX])
    monkeypatch.setattr("rutherford.tools.setup.install_adapter", fake_install)
    payload = decode(await setup_tool(app, install_adapters=True))
    assert installed_ids == ["codex"]
    assert payload["adapters"]["installed"] == [
        {
            "agent": "codex",
            "package": "@agentclientprotocol/codex-acp",
            "ok": True,
            "detail": "installed @agentclientprotocol/codex-acp",
        }
    ]


def pytest_fail_if_called() -> AdapterInstall:
    raise AssertionError("install_adapter must not run when install_adapters is false")
