# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Detect and set up the npm ACP adapter shims that front an underlying CLI.

A few agents launch a SEPARATE npm-installed adapter -- ``codex`` -> ``codex-acp``, ``claude_code`` ->
``claude-agent-acp``, ``pi`` -> ``pi-acp`` -- that fronts the real CLI (``codex`` / ``claude`` / ``pi``) as
an ACP server. When the underlying CLI is installed but the adapter shim is not, the agent probes as a flat
``not_installed`` even though the user clearly has the tool. This module recognizes that case so ``doctor`` can
report an installable gap with the exact ``npm i -g`` command, and ``setup`` can run the install on request.

Two safety properties: the install argv is built from the descriptor's CURATED ``adapter_package`` constant,
never from caller input, so it cannot inject; and installing (an ``npm i -g`` that mutates the machine) is an
explicit, opt-in action -- it is NEVER run during a read-only probe.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .descriptors import AgentDescriptor, DescriptorRegistry
from .launch import prepare_argv

#: How long an ``npm i -g`` may run before it is abandoned (a cold install pulls the package + deps).
_INSTALL_TIMEOUT_S = 300.0

#: Injectable PATH lookup (``shutil.which``) so the detection logic is unit-testable without a real PATH.
Which = Callable[[str], str | None]


class AdapterState(StrEnum):
    """The install state of an agent's ACP adapter shim relative to its underlying CLI."""

    #: The agent IS its own ACP server (goose, gemini, ...): there is no separate shim to set up.
    NOT_APPLICABLE = "not_applicable"
    #: The adapter shim is on PATH -- nothing to do.
    INSTALLED = "installed"
    #: The shim is missing but the underlying CLI is present -- installable with one ``npm i -g``.
    CLI_PRESENT = "cli_present"
    #: Neither the shim nor the underlying CLI is present -- install the CLI itself first.
    CLI_ABSENT = "cli_absent"


@dataclass(frozen=True)
class AdapterInstall:
    """The outcome of installing one agent's adapter shim."""

    agent_id: str
    package: str
    ok: bool
    detail: str


def adapter_install_command(descriptor: AgentDescriptor) -> tuple[str, ...] | None:
    """The ``npm i -g <package>`` argv for an agent's adapter, or ``None`` when it has no separate shim."""
    if not descriptor.is_wrapped_adapter:
        return None
    return ("npm", "i", "-g", descriptor.adapter_package or "")


def adapter_state(descriptor: AgentDescriptor, *, which: Which = shutil.which) -> AdapterState:
    """Classify whether an agent's adapter shim is installed, installable, or blocked on its CLI."""
    if not descriptor.is_wrapped_adapter:
        return AdapterState.NOT_APPLICABLE
    if which(descriptor.command[0]) is not None:
        return AdapterState.INSTALLED
    if descriptor.underlying_cli is not None and which(descriptor.underlying_cli) is not None:
        return AdapterState.CLI_PRESENT
    return AdapterState.CLI_ABSENT


def install_hint(descriptor: AgentDescriptor, *, which: Which = shutil.which) -> str | None:
    """A one-line install instruction when an agent's adapter shim is missing but its CLI is present.

    ``None`` for an agent that needs no shim, has its shim, or is missing the underlying CLI too (a generic
    "install the agent" message already covers that). Shown by ``doctor`` next to the ``not_installed`` status.
    """
    if adapter_state(descriptor, which=which) is not AdapterState.CLI_PRESENT:
        return None
    return (
        f"the {descriptor.underlying_cli!r} CLI is installed but its ACP adapter {descriptor.command[0]!r} is "
        f"not -- install it with `npm i -g {descriptor.adapter_package}`, or `setup install_adapters=true`"
    )


def installable_adapters(registry: DescriptorRegistry, *, which: Which = shutil.which) -> list[AgentDescriptor]:
    """Every wrapped-adapter agent whose shim is missing but whose underlying CLI is present (installable)."""
    return [d for d in registry.all() if adapter_state(d, which=which) is AdapterState.CLI_PRESENT]


def install_adapter(
    descriptor: AgentDescriptor,
    *,
    which: Which = shutil.which,
    runner: Callable[[tuple[str, ...]], tuple[bool, str]] | None = None,
) -> AdapterInstall:
    """Install an agent's adapter shim via ``npm i -g <package>`` (opt-in; mutates the machine).

    ``runner`` is injectable so a test never shells out to npm. Refuses cleanly when the agent has no separate
    adapter or npm is not installed, so a caller never sees an uncaught spawn error.
    """
    package = descriptor.adapter_package
    if not descriptor.is_wrapped_adapter or package is None:
        return AdapterInstall(descriptor.id, "", False, "this agent has no separate npm adapter to install")
    if which("npm") is None:
        return AdapterInstall(descriptor.id, package, False, "npm is not on PATH; install Node.js / npm first")
    run = runner or _run_npm
    ok, detail = run(("npm", "i", "-g", package))
    return AdapterInstall(descriptor.id, package, ok, detail)


def _run_npm(cmd: tuple[str, ...]) -> tuple[bool, str]:
    """Run an ``npm`` command (resolving the Windows shim via :func:`prepare_argv`); return (ok, detail)."""
    # argv is ``npm`` + a curated ``adapter_package`` constant (never caller input), resolved through the same
    # launch path the agent spawns use, so it cannot inject a shell or an arbitrary command.
    argv = prepare_argv(cmd)
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=_INSTALL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:  # npm vanished mid-call, or the install timed out
        return False, f"npm install failed: {exc}"
    if completed.returncode == 0:
        return True, f"installed {cmd[-1]}"
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return False, f"npm exited {completed.returncode}: {tail[-1] if tail else 'unknown error'}"
