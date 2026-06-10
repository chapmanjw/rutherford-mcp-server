# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared adapter probing for the ``capabilities`` and ``doctor`` tools.

Builds an :class:`~rutherford.domain.models.AdapterStatus` for one adapter by running its
non-destructive metadata methods (``detect``, ``check_auth``, ``available_models``,
``capabilities``). ``doctor`` additionally asks for human-readable diagnostic notes.
"""

from __future__ import annotations

import re

from ..adapters.base import CLIAdapter
from ..domain.enums import AuthState
from ..domain.models import AdapterStatus, AuthStatus

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


def version_token(text: str | None) -> str | None:
    """Extract a ``MAJOR.MINOR.PATCH`` token from a version string, or ``None``.

    A CLI's ``--version`` line is unstructured (``agy 1.0.8 (build abc)``, ``1.0.8``, ...), so compare
    the extracted semver token, not the raw line -- a version-string *format* change must not read as a
    version *bump*.
    """
    if not text:
        return None
    match = _SEMVER_RE.search(text)
    return match.group(0) if match else None


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

    notes = _diagnose(adapter, detected.installed, detected.version, auth, models, optional) if diagnostic else []
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
    version: str | None,
    auth: AuthStatus,
    models: list[str],
    optional: bool,
) -> list[str]:
    """Produce notes for an unavailable, unauthenticated, not-ready, or version-drifted target.

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
    # Version-drift canary: an adapter whose flags or output layout were verified against a specific
    # CLI version (e.g. Antigravity's reverse-engineered transcript schema) flags when the installed
    # binary differs from the pin, so a silent upstream change becomes a visible prompt to re-verify.
    # Compared on the extracted semver token so a --version *format* change is not mistaken for a bump.
    verified = getattr(adapter, "verified_version", None)
    running = version_token(version)
    if verified and running and running != version_token(verified):
        notes.append(
            f"{binary} is at {version}, but its behavior was verified against {verified}; re-verify "
            "and re-pin if results look wrong (the output layout may have changed under an auto-update)"
        )
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
