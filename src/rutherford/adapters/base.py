# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``CLIAdapter`` interface -- the heart of the project -- and a shared base class.

Every CLI implements :class:`CLIAdapter`; the core knows nothing else about any CLI. Two methods
carry the contract that keeps the design clean:

* ``build_invocation`` is **pure** and returns an :class:`InvocationSpec` (an argv list, env,
  cwd, and optional stdin). It never builds or returns a shell string.
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
from typing import Protocol, runtime_checkable

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
    Provenance,
    SafetyFlags,
)
from ..runtime.probe import CommandProbe, SystemProbe
from .provenance import infer_provider_from_model


@runtime_checkable
class CLIAdapter(Protocol):
    """The interface every CLI adapter satisfies and the only adapter type the core depends on."""

    id: str
    display_name: str
    #: Whether the adapter is opt-in (a local model the user need not run). An absent optional
    #: adapter reads as "only if you want it", and it is excluded from an auto-``all`` panel.
    optional: bool

    def detect(self) -> DetectResult:
        """Report whether the binary is installed and runnable (path and version, or absent)."""
        ...

    def check_auth(self) -> AuthStatus:
        """Probe auth state without ever triggering a login."""
        ...

    def available_models(self) -> list[str]:
        """List selectable models, querying the CLI where possible, else a static set."""
        ...

    def fallback_model(self) -> str | None:
        """The model to retry with when a requested model is unavailable (``None`` = default)."""
        ...

    def capabilities(self) -> AdapterCapabilities:
        """Advertise feature flags so the core can adapt behavior generically."""
        ...

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Pure mapping from a normalized request to an argv list, env, cwd, and optional stdin."""
        ...

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Translate the universal SafetyMode to this CLI's approval/sandbox flags."""
        ...

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope, including on failure."""
        ...

    def provenance(self, ctx: InvocationContext) -> Provenance:
        """Best-effort provider/model identity for a delegation to this CLI (F3, without ``cli_version``).

        Returns who served the answer and which model, so the service can stamp it on the result and
        consensus/debate can report effective diversity. ``cli_version`` is filled in by the service
        (it has the detected version cheaply); this returns provider/backend/model/confirmed.
        """
        ...

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """Whether ``raw`` matches this adapter's expected successful-output shape (a drift canary).

        Checked centrally only when ``parse_output`` already returned ``ok``; a ``False`` there fails
        the result with ``CONTRACT_MISMATCH`` so silent output drift (the CLI's machine-readable
        format changing underneath the adapter) becomes a loud failure rather than a trusted result.
        """
        ...


class BaseCLIAdapter(ABC):
    """Shared scaffolding for concrete adapters.

    Concrete adapters set these as class attributes (``id``, ``display_name``, ``binary``, and
    optionally ``static_models`` / ``version_args``) and implement the genuinely CLI-specific
    methods (``check_auth``, ``capabilities``, ``build_invocation``, ``map_safety``,
    ``parse_output``). They are plain class attributes (not ``ClassVar``) that each concrete adapter
    sets directly. A :class:`~rutherford.runtime.probe.CommandProbe` is injected for testability.
    """

    id: str
    display_name: str
    binary: str
    static_models: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    #: True for an adapter the user only needs if they opt in (e.g. a local model). Surfaced by
    #: ``capabilities``/``doctor`` so an absent one reads as optional, not as a missing requirement.
    optional: bool = False
    #: The model vendor this CLI serves when it is a fixed property of the CLI (e.g. ``"openai"`` for
    #: Codex, ``"local"`` for Ollama). ``None`` means the provider depends on the model or backend and
    #: the adapter derives it in :meth:`provenance` (or it is unknown). See F3.
    provider: str | None = None
    #: Whether :attr:`provider` is a definitive fact (a fixed-vendor CLI, ``True``) or a best-guess
    #: home-vendor default the actual backend could override (``False``). Surfaced as
    #: ``Provenance.confirmed`` so a reader can tell a certain provider from an inferred one.
    provider_confirmed: bool = False

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

    def fallback_model(self) -> str | None:
        """No distinct fallback by default; the delegation service retries with the default model.

        An adapter whose default model can be unavailable (for example Cursor, where a free plan
        rejects named models) overrides this to return an always-available model id (``"auto"``).
        """
        return None

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """Assume the output contract holds. Adapters with a known machine-readable shape override.

        The default is lenient on purpose: most adapters have no contract beyond "exit 0 with text",
        and a false ``False`` here would fail a genuine success. An adapter with a structured output
        (Claude's JSON envelope, Codex's JSONL event stream) overrides this to assert that shape, so
        the delegation service can fail a drifted-but-ok result with ``CONTRACT_MISMATCH``.
        """
        return True

    def provenance(self, ctx: InvocationContext) -> Provenance:
        """Best-effort provider/model identity (F3): the confirmed :attr:`provider`, else model evidence.

        Resolution order: a *confirmed* fixed vendor wins (a fixed-vendor CLI, ``provider_confirmed``);
        otherwise a vendor recognized from the model id wins over an unconfirmed home-vendor default
        (so a Codex run pointed at a ``claude``/``anthropic.`` model is not mislabeled ``openai``);
        otherwise the unconfirmed home default; otherwise unknown. An adapter whose provider depends on
        an env switch, a ``provider/model`` namespace, or config overrides this. ``cli_version`` is
        filled in by the service.
        """
        model = ctx.target.model
        if self.provider is not None and self.provider_confirmed:
            return Provenance(provider=self.provider, model=model, confirmed=True)
        inferred = infer_provider_from_model(model)
        if inferred is not None:
            return Provenance(provider=inferred, model=model, confirmed=False)
        return Provenance(provider=self.provider, model=model, confirmed=False)

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
    def _with_files(prompt: str, files: list[str]) -> str:
        """Append an in-scope file list to the prompt, for CLIs without a file-attach flag."""
        if not files:
            return prompt
        listing = "\n".join(f"- {path}" for path in files)
        return f"{prompt}\n\nFiles in scope:\n{listing}"

    @staticmethod
    def _compose_prompt(prompt: str, preamble: str | None) -> str:
        """Prepend a role preamble to the prompt, for CLIs without a system-prompt flag."""
        if not preamble:
            return prompt
        return f"{preamble}\n\n---\n\n{prompt}"

    @staticmethod
    def _env_present(*names: str) -> str | None:
        """Return the first of ``names`` set to a non-empty value in the environment."""
        for name in names:
            if os.environ.get(name):
                return name
        return None

    @staticmethod
    def _env_value(*names: str) -> str | None:
        """Return the first non-empty *value* among ``names`` in the environment, or ``None``.

        Like :meth:`_env_present` but yields the value, not the variable name -- for a config carried
        in an env var whose content matters (e.g. ``GOOSE_PROVIDER`` naming the provider).
        """
        for name in names:
            value = os.environ.get(name)
            if value and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _env_truthy(*names: str) -> bool:
        """Return whether any of ``names`` is set to a truthy toggle value.

        Flag-style env vars (``CLAUDE_CODE_USE_BEDROCK=1``) are opt-in switches: an explicit
        ``0`` / ``false`` / ``no`` / ``off`` (or an empty value) means off; any other non-empty
        value means on. Used to detect a third-party model backend (Bedrock/Vertex) that the
        cheap auth probe cannot verify on its own.
        """
        falsy = {"", "0", "false", "no", "off"}
        for name in names:
            value = os.environ.get(name)
            if value is not None and value.strip().lower() not in falsy:
                return True
        return False

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
