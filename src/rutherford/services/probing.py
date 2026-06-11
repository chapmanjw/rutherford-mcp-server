# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Adapter probing for the ``capabilities``, ``doctor``, and ``setup`` tools.

Builds an :class:`~rutherford.domain.models.AdapterStatus` for one adapter by running its
non-destructive metadata methods (``detect``, ``check_auth``, ``available_models``,
``capabilities``). ``doctor`` additionally asks for human-readable diagnostic notes and can
confirm an unprobeable adapter's auth with a minimal live round trip.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from ..adapters.base import CLIAdapter
from ..adapters.registry import AdapterRegistry
from ..domain.enums import AuthState
from ..domain.models import AdapterCapabilities, AdapterStatus, AuthStatus, DelegationRequest, Target
from .delegation import DelegationService

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")

#: A tiny read-only prompt used by the live auth check.
_LIVE_AUTH_PROMPT = "Reply with exactly the two characters: ok"


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

    notes = (
        _diagnose(adapter, detected.installed, detected.version, auth, models, optional, capabilities)
        if diagnostic
        else []
    )
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
        notes=notes,
    )


async def probe_all(
    registry: AdapterRegistry,
    *,
    diagnostic: bool = False,
    default_model_for: Callable[[str], str | None] | None = None,
) -> list[AdapterStatus]:
    """Probe every registered adapter and return their statuses, ordered by id.

    Probes run in worker threads so the synchronous metadata calls (version, list-models,
    auth-status) do not block the event loop. ``default_model_for`` (the config lookup) stamps each
    status with the model a no-model delegation would use.
    """
    statuses = list(
        await asyncio.gather(
            *(asyncio.to_thread(probe_adapter, adapter, diagnostic=diagnostic) for adapter in registry.all())
        )
    )
    if default_model_for is not None:
        for status in statuses:
            status.default_model = default_model_for(status.id)
    return statuses


async def verify_live(
    delegation: DelegationService,
    statuses: list[AdapterStatus],
    *,
    correlation_id_factory: Callable[[], str],
    base_depth: int,
) -> list[AdapterStatus]:
    """Confirm each installed-but-unknown adapter's auth with a minimal read-only delegation.

    The only trustworthy signal for an adapter with no non-interactive auth check is a real round
    trip; every other status passes through untouched.
    """
    return list(
        await asyncio.gather(
            *(
                _verify_one(delegation, status, correlation_id_factory=correlation_id_factory, base_depth=base_depth)
                for status in statuses
            )
        )
    )


async def _verify_one(
    delegation: DelegationService,
    status: AdapterStatus,
    *,
    correlation_id_factory: Callable[[], str],
    base_depth: int,
) -> AdapterStatus:
    if not (status.installed and status.auth.state is AuthState.UNKNOWN):
        return status
    request = DelegationRequest(target=Target(cli=status.id), prompt=_LIVE_AUTH_PROMPT, timeout_s=60)
    result = await delegation.delegate(request, correlation_id=correlation_id_factory(), base_depth=base_depth)
    kept = [note for note in status.notes if "could not be verified" not in note]
    if result.ok:
        status.auth = AuthStatus(state=AuthState.AUTHENTICATED, detail="verified by a live round trip")
        status.notes = [*kept, "auth confirmed by a live invocation"]
    else:
        detail = result.error.message if result.error else "live auth check failed"
        status.auth = AuthStatus(state=AuthState.NEEDS_LOGIN, detail=detail)
        status.notes = [*kept, "a live auth check failed; sign in to the CLI interactively"]
    return status


def _diagnose(
    adapter: CLIAdapter,
    installed: bool,
    version: str | None,
    auth: AuthStatus,
    models: list[str],
    optional: bool,
    capabilities: AdapterCapabilities,
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
    if capabilities.write_uses_bypass:
        notes.append(
            f"{binary} has no write posture distinct from its permission bypass: write and yolo are "
            "equivalent on this adapter (both use the bypass flag, gated by the trusted-workspace check). "
            "Request yolo when you intend the bypass; treat write delegations here accordingly."
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
