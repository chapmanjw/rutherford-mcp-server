# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``CLIAdapter`` interface -- the heart of the project -- and a shared base class.

Every CLI implements :class:`CLIAdapter`; the core knows nothing else about any CLI. Two methods
carry the contract that keeps the design clean:

* ``build_invocation`` is **pure** and returns an :class:`InvocationSpec` (an argv list, env,
  cwd, and runtime hint). It never builds or returns a shell string.
* ``parse_output`` maps the raw process result to the normalized :class:`DelegationResult`
  envelope, and is where every CLI-specific quirk lives (for example, reading a transcript file
  instead of trusting stdout). Quirks must not leak upward.

:class:`BaseCLIAdapter` provides the boilerplate shared by the concrete adapters -- ``detect``
via the injected :class:`~rutherford.runtime.probe.CommandProbe`, a default ``available_models``,
and small auth/version helpers -- so an adapter implements only what is genuinely CLI-specific.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import ClassVar, Protocol, runtime_checkable

from ..domain.enums import AuthState, SafetyMode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    DelegationRequest,
    DelegationResult,
    DetectResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)
from ..runtime.probe import CommandProbe, SystemProbe


@runtime_checkable
class CLIAdapter(Protocol):
    """The interface every CLI adapter satisfies and the only adapter type the core depends on."""

    id: str
    display_name: str

    def detect(self) -> DetectResult:
        """Report whether the binary is installed and runnable (path and version, or absent)."""
        ...

    def check_auth(self) -> AuthStatus:
        """Probe auth state without ever triggering a login."""
        ...

    def available_models(self) -> list[str]:
        """List selectable models, querying the CLI where possible, else a static set."""
        ...

    def capabilities(self) -> AdapterCapabilities:
        """Advertise feature flags so the core can adapt behavior generically."""
        ...

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Pure mapping from a normalized request to an argv list, env, cwd, and runtime hint."""
        ...

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Translate the universal SafetyMode to this CLI's approval/sandbox flags."""
        ...

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope, including on failure."""
        ...


class BaseCLIAdapter(ABC):
    """Shared scaffolding for concrete adapters.

    Subclasses set the class attributes (``id``, ``display_name``, ``binary``, and optionally
    ``static_models`` / ``version_args``) and implement the genuinely CLI-specific methods
    (``check_auth``, ``capabilities``, ``build_invocation``, ``map_safety``, ``parse_output``).
    A :class:`~rutherford.runtime.probe.CommandProbe` is injected for testability.
    """

    id: ClassVar[str]
    display_name: ClassVar[str]
    binary: ClassVar[str]
    static_models: ClassVar[tuple[str, ...]] = ()
    version_args: ClassVar[tuple[str, ...]] = ("--version",)

    def __init__(self, probe: CommandProbe | None = None) -> None:
        self._probe: CommandProbe = probe if probe is not None else SystemProbe()

    # --- default implementations --------------------------------------------

    def detect(self) -> DetectResult:
        """Resolve the binary on PATH and read its version. Never triggers a login."""
        path = self._probe.which(self.binary)
        if path is None:
            return DetectResult(installed=False)
        return DetectResult(installed=True, path=path, version=self._detect_version())

    def available_models(self) -> list[str]:
        """Return the static model set. Adapters with a list-models command override this."""
        return list(self.static_models)

    # --- helpers for subclasses ---------------------------------------------

    def _detect_version(self) -> str | None:
        """Run the version command and return its first non-empty line, or ``None``."""
        result = self._probe.run([self.binary, *self.version_args], timeout_s=15.0)
        if result.exit_code != 0:
            return None
        text = (result.stdout or result.stderr).strip()
        if not text:
            return None
        return text.splitlines()[0].strip()

    @staticmethod
    def _env_present(*names: str) -> str | None:
        """Return the first of ``names`` set to a non-empty value in the environment."""
        for name in names:
            if os.environ.get(name):
                return name
        return None

    def _auth_from_env_or_command(
        self,
        env_vars: tuple[str, ...],
        status_argv: list[str] | None = None,
    ) -> AuthStatus:
        """Resolve auth state from an API-key env var, then an optional status command.

        If any of ``env_vars`` is set, report ``AUTHENTICATED``. Otherwise, if ``status_argv``
        is given, run it and treat exit 0 as an existing persisted session. Falling through
        means a login is needed (or, with no env key and no status command, ``UNKNOWN``).
        """
        present = self._env_present(*env_vars)
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        if status_argv is not None:
            result = self._probe.run(status_argv, timeout_s=15.0)
            if result.exit_code == 0:
                return AuthStatus(state=AuthState.AUTHENTICATED, detail="persisted session detected")
            return AuthStatus(state=AuthState.NEEDS_LOGIN, detail="no session; interactive login required")
        if env_vars:
            joined = " or ".join(env_vars)
            return AuthStatus(state=AuthState.API_KEY_MISSING, detail=f"set {joined} or log in")
        return AuthStatus(state=AuthState.UNKNOWN)

    # --- abstract: genuinely CLI-specific -----------------------------------

    @abstractmethod
    def check_auth(self) -> AuthStatus:
        """Probe auth state without triggering a login."""

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Advertise this adapter's feature flags."""

    @abstractmethod
    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Pure mapping from request to invocation. Must never build a shell string."""

    @abstractmethod
    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to this CLI's flags, defaulting conservatively."""

    @abstractmethod
    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope, including on non-zero exit."""
