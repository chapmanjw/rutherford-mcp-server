# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The result-envelope helpers and the application context.

Mirrors the owner's ``toolSuccess`` / ``toolError`` pair: one helper to build a success payload
and one to build an error payload, so every tool returns an identically shaped, TOON-encoded
result. The :class:`AppContext` holds the long-lived services built once at startup and mints
correlation ids (:meth:`AppContext.new_correlation_id`); per-call values -- the correlation id,
timeout, safety mode -- travel as explicit arguments through the tool and service layers.

These helpers return strings (the TOON text a FastMCP tool returns as a text block). Whether an
error payload is returned normally or raised as an MCP error is the thin tool layer's decision,
which keeps this module independent of the transport.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .adapters.registry import AdapterRegistry, build_registry
from .config.loader import load_config
from .config.panels import PanelCache, load_panels
from .config.schema import RutherfordConfig
from .domain.error_codes import ErrorCode
from .domain.errors import RutherfordError
from .io.serialize import encode
from .runtime.depth import current_depth
from .runtime.probe import SystemProbe
from .runtime.probe_cache import CachingProbe
from .runtime.process import AsyncProcessRunner, ProcessRunner
from .services.consensus import ConsensusService
from .services.debate import DebateService
from .services.delegation import DelegationService
from .services.jobs import JobService, JobStore
from .services.roles import RoleStore, load_roles


def tool_success(data: Any) -> str:
    """Build a success payload: ``data`` serialized through the TOON seam."""
    return encode(data)


def tool_error(code: ErrorCode | str, message: str, details: dict[str, Any] | None = None) -> str:
    """Build an error payload carrying a stable error code, serialized through the TOON seam."""
    error: dict[str, Any] = {"code": str(code), "message": message}
    if details:
        error["details"] = details
    return encode({"error": error})


def error_payload_from(exc: RutherfordError) -> str:
    """Build an error payload from a :class:`RutherfordError`."""
    return tool_error(exc.code, exc.message, exc.details)


@dataclass(slots=True)
class AppContext:
    """The long-lived services and state, built once at startup and shared across tool calls."""

    config: RutherfordConfig
    registry: AdapterRegistry
    roles: RoleStore
    panels: PanelCache
    delegation: DelegationService
    consensus: ConsensusService
    debate: DebateService
    jobs: JobService
    #: The depth this server runs at, read from ``RUTHERFORD_DEPTH`` when it was spawned.
    base_depth: int = 0
    #: The caching probe wrapping the adapters' metadata calls, when one was built (``None`` when a
    #: registry was injected, e.g. in tests). ``doctor`` invalidates it before a live re-check.
    probe_cache: CachingProbe | None = None

    def new_correlation_id(self) -> str:
        """Mint a short correlation id for a tool call."""
        return uuid.uuid4().hex[:12]


def build_app_context(
    *,
    config: RutherfordConfig | None = None,
    runner: ProcessRunner | None = None,
    registry: AdapterRegistry | None = None,
    roles: RoleStore | None = None,
    panels: PanelCache | None = None,
    base_depth: int | None = None,
) -> AppContext:
    """Assemble the application context: load config, build the registry and services.

    Arguments are injectable for tests; in production all default to the real implementations
    (config discovered from disk and environment, the asyncio process runner, the depth read from
    ``RUTHERFORD_DEPTH``).
    """
    resolved_config = config if config is not None else load_config()
    resolved_runner = runner if runner is not None else AsyncProcessRunner()
    resolved_depth = current_depth() if base_depth is None else base_depth

    probe_cache: CachingProbe | None = None
    if registry is not None:
        resolved_registry = registry
    else:
        probe_cache = CachingProbe(
            SystemProbe(),
            ttl_s=resolved_config.probe_cache_ttl_s,
            ceiling_s=resolved_config.probe_timeout_s,
        )
        resolved_registry = build_registry(resolved_config, probe=probe_cache)
    resolved_roles = roles if roles is not None else load_roles(resolved_config.role_dirs)
    resolved_panels = panels if panels is not None else PanelCache(lambda: load_panels(resolved_registry.ids()))
    delegation = DelegationService(resolved_registry, resolved_runner, resolved_config, resolved_roles)
    consensus = ConsensusService(delegation, resolved_config, resolved_registry)
    debate = DebateService(delegation, resolved_config)
    jobs = JobService(JobStore(ttl_s=resolved_config.job_ttl_s, max_jobs=resolved_config.max_jobs))
    return AppContext(
        config=resolved_config,
        registry=resolved_registry,
        roles=resolved_roles,
        panels=resolved_panels,
        delegation=delegation,
        consensus=consensus,
        debate=debate,
        jobs=jobs,
        base_depth=resolved_depth,
        probe_cache=probe_cache,
    )
