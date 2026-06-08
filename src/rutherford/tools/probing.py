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
    optional = adapter.optional
    if detected.installed:
        auth = adapter.check_auth()
        try:
            models = adapter.available_models()
        except Exception:  # a flaky list-models probe must not break capabilities/doctor
            models = []
    else:
        auth = AuthStatus(state=AuthState.UNKNOWN, detail="not installed")
        models = []

    notes = _diagnose(adapter, detected.installed, auth, models, optional) if diagnostic else []
    return AdapterStatus(
        id=adapter.id,
        display_name=adapter.display_name,
        installed=detected.installed,
        optional=optional,
        path=detected.path,
        version=detected.version,
        auth=auth,
        models=models,
        capabilities=capabilities,
        runtime=capabilities.runtime,
        notes=notes,
    )


def _diagnose(
    adapter: CLIAdapter,
    installed: bool,
    auth: AuthStatus,
    models: list[str],
    optional: bool,
) -> list[str]:
    """Produce notes for an unavailable, unauthenticated, or not-ready target.

    An ``optional`` adapter (a local model the user need not run) is never framed as a missing
    requirement: its absence or empty model list reads as "only if you want it", not as an error.
    """
    binary = getattr(adapter, "binary", adapter.id)
    if not installed:
        if optional:
            return [
                f"optional: a local model via {binary}. Install it and pull a model "
                f"(e.g. `{binary} pull <model>`) only if you want local delegation; otherwise ignore."
            ]
        return [f"{binary} was not found on PATH; install it (see docs/integration-testing.md)"]
    notes: list[str] = []
    if auth.state is AuthState.NEEDS_LOGIN:
        notes.append("not authenticated; run the CLI's own login once (Rutherford never logs in for you)")
    elif auth.state is AuthState.API_KEY_MISSING:
        notes.append(f"no credential found: {auth.detail}")
    elif auth.state is AuthState.UNKNOWN and auth.detail:
        notes.append(f"auth state could not be verified non-interactively: {auth.detail}")
    if optional and not models:
        notes.append(
            f"optional: {binary} is installed but no models are available -- start the daemon "
            f"(e.g. `{binary} serve`) and pull a model only if you want local delegation."
        )
    return notes
