# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``capabilities`` and ``doctor`` tools."""

from __future__ import annotations

import asyncio

from ..context import AppContext, tool_success
from ..domain.enums import AuthState
from ..domain.models import AdapterStatus, AuthStatus, DelegationRequest, Target
from .probing import probe_adapter

#: A tiny read-only prompt used by the live auth check.
_LIVE_AUTH_PROMPT = "Reply with exactly the two characters: ok"


async def capabilities_tool(app: AppContext) -> str:
    """List every known CLI: whether it is installed, its auth status, and its models.

    Probes run in worker threads so the metadata calls (version, list-models, auth-status) do not
    block the event loop.
    """
    adapters = app.registry.all()
    statuses = await asyncio.gather(*(asyncio.to_thread(probe_adapter, adapter) for adapter in adapters))
    return tool_success(list(statuses))


async def doctor_tool(app: AppContext, *, live: bool = True) -> str:
    """Health-probe every adapter, verifying auth that cannot be checked cheaply.

    `doctor` confirms each CLI is installed and reads its auth state. Some CLIs (Antigravity) have
    no non-interactive auth check, so their cheap state is ``unknown``. With ``live=True`` (the
    default), any installed adapter that is still ``unknown`` is verified with a minimal read-only
    round trip and reclassified by the outcome -- the only trustworthy signal when there is no
    ``whoami``. That spends a small model call for each such adapter; pass ``live=False`` for a
    metadata-only check with no model calls (``capabilities`` is the always-cheap snapshot).
    """
    adapters = app.registry.all()
    statuses = await asyncio.gather(
        *(asyncio.to_thread(probe_adapter, adapter, diagnostic=True) for adapter in adapters)
    )
    status_list = list(statuses)
    if live:
        status_list = await asyncio.gather(*(_verify_live(app, status) for status in status_list))
    payload = {
        "adapters": list(status_list),
        "depth": app.base_depth,
        "max_depth": app.config.max_depth,
        "max_targets": app.config.max_targets,
        "default_safety_mode": app.config.default_safety_mode,
    }
    return tool_success(payload)


async def _verify_live(app: AppContext, status: AdapterStatus) -> AdapterStatus:
    """Confirm an installed-but-unknown adapter's auth with a minimal read-only delegation."""
    if not (status.installed and status.auth.state is AuthState.UNKNOWN):
        return status
    request = DelegationRequest(target=Target(cli=status.id), prompt=_LIVE_AUTH_PROMPT, timeout_s=60)
    result = await app.delegation.delegate(request, correlation_id=app.new_correlation_id(), base_depth=app.base_depth)
    kept = [note for note in status.notes if "could not be verified" not in note]
    if result.ok:
        status.auth = AuthStatus(state=AuthState.AUTHENTICATED, detail="verified by a live round trip")
        status.notes = [*kept, "auth confirmed by a live invocation"]
    else:
        detail = result.error.message if result.error else "live auth check failed"
        status.auth = AuthStatus(state=AuthState.NEEDS_LOGIN, detail=detail)
        status.notes = [*kept, "a live auth check failed; sign in to the CLI interactively"]
    return status
