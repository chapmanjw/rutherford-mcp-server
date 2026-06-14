# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Result-envelope helpers and the application context (ACP-native).

``tool_success`` / ``tool_error`` build the identically shaped, TOON-encoded payload every tool returns;
the :class:`AppContext` holds the long-lived state built once at startup -- the validated config, the agent
:class:`~rutherford.acp.descriptors.DescriptorRegistry`, and the :class:`DelegationService`. Per-call values
(correlation id, timeout, safety mode) travel as explicit arguments through the tool and service layers, so
this module stays independent of the transport.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .acp.cooldown import CooldownTracker
from .acp.descriptors import DescriptorRegistry
from .acp.roster import build_registry
from .config.loader import load_config
from .config.panels import PanelCache, load_panels
from .config.schema import RutherfordConfig
from .domain.error_codes import ErrorCode
from .domain.errors import RutherfordError
from .io.serialize import encode
from .services.consensus import ConsensusService
from .services.debate import DebateService
from .services.delegation import DelegationService
from .services.jobs import JobStore
from .services.roles import RoleStore


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
    """The long-lived state and services, built once at startup and shared across tool calls."""

    config: RutherfordConfig
    descriptors: DescriptorRegistry
    delegation: DelegationService
    consensus: ConsensusService
    debate: DebateService
    jobs: JobStore
    roles: RoleStore
    panels: PanelCache

    def new_correlation_id(self) -> str:
        """Mint a short correlation id for a tool call."""
        return uuid.uuid4().hex[:12]


def build_app_context(
    *,
    config: RutherfordConfig | None = None,
    descriptors: DescriptorRegistry | None = None,
) -> AppContext:
    """Assemble the application context: load config, build the descriptor registry and the service.

    Arguments are injectable for tests; in production both default to the real implementations (config
    discovered from disk and environment, the descriptor roster built from the built-in defaults plus any
    ``[agents.<id>]`` config).
    """
    resolved_config = config if config is not None else load_config()
    resolved_descriptors = descriptors if descriptors is not None else build_registry(resolved_config)
    # The cooldown tracker (F7) is process-global state shared by the delegation primitive (which records each
    # turn's health) and the consensus service (which skips a benched agent in an auto-expanded panel), so it
    # is built once here and injected into both -- the two paths must read the SAME bench state.
    cooldown = CooldownTracker(
        threshold=resolved_config.cooldown_threshold,
        window_s=resolved_config.cooldown_window_s,
        duration_s=resolved_config.cooldown_duration_s,
    )
    delegation = DelegationService(resolved_descriptors, resolved_config, cooldown=cooldown)
    consensus = ConsensusService(delegation, resolved_descriptors, resolved_config, cooldown=cooldown)
    debate = DebateService(resolved_descriptors, resolved_config, delegation)
    jobs = JobStore(max_jobs=resolved_config.max_jobs, job_ttl_s=resolved_config.job_ttl_s)
    roles = RoleStore(role_dirs=resolved_config.role_dirs)
    # Panels are validated against the LIVE registry ids, so a panel naming an unknown agent fails to load.
    # Loading is lazy (PanelCache loads on first use / reload), so a malformed panels file does not break
    # startup until a panel is actually used or ``reload_panels`` is called.
    panels = PanelCache(lambda: load_panels(resolved_descriptors.ids()))
    return AppContext(
        config=resolved_config,
        descriptors=resolved_descriptors,
        delegation=delegation,
        consensus=consensus,
        debate=debate,
        jobs=jobs,
        roles=roles,
        panels=panels,
    )
