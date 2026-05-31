# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared adapter probing for the ``capabilities`` and ``doctor`` tools.

Builds an :class:`~rutherford.domain.models.AdapterStatus` for one adapter by running its
non-destructive metadata methods (``detect``, ``check_auth``, ``available_models``,
``capabilities``). ``doctor`` additionally asks for human-readable diagnostic notes.
"""

from __future__ import annotations

from ..adapters.base import CLIAdapter
from ..domain.enums import AuthState
from ..domain.models import AdapterStatus, AuthStatus


def probe_adapter(adapter: CLIAdapter, *, diagnostic: bool = False) -> AdapterStatus:
    """Build an :class:`AdapterStatus` for ``adapter``. Never raises on a probe failure."""
    detected = adapter.detect()
    capabilities = adapter.capabilities()
    if detected.installed:
        auth = adapter.check_auth()
        try:
            models = adapter.available_models()
        except Exception:  # a flaky list-models probe must not break capabilities/doctor
            models = []
    else:
        auth = AuthStatus(state=AuthState.UNKNOWN, detail="not installed")
        models = []

    notes = _diagnose(adapter, detected.installed, auth) if diagnostic else []
    return AdapterStatus(
        id=adapter.id,
        display_name=adapter.display_name,
        installed=detected.installed,
        path=detected.path,
        version=detected.version,
        auth=auth,
        models=models,
        capabilities=capabilities,
        runtime=capabilities.runtime,
        notes=notes,
    )


def _diagnose(adapter: CLIAdapter, installed: bool, auth: AuthStatus) -> list[str]:
    """Produce actionable notes for an unavailable or unauthenticated target."""
    if not installed:
        binary = getattr(adapter, "binary", adapter.id)
        return [f"{binary} was not found on PATH; install it (see docs/integration-testing.md)"]
    notes: list[str] = []
    if auth.state is AuthState.NEEDS_LOGIN:
        notes.append("not authenticated; run the CLI's own login once (Rutherford never logs in for you)")
    elif auth.state is AuthState.API_KEY_MISSING:
        notes.append(f"no credential found: {auth.detail}")
    elif auth.state is AuthState.UNKNOWN and auth.detail:
        notes.append(f"auth state could not be verified non-interactively: {auth.detail}")
    return notes
