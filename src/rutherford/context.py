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
from pathlib import Path
from typing import Any

from .acp.cooldown import CooldownTracker
from .acp.descriptors import DescriptorRegistry
from .acp.roster import build_registry
from .config.loader import has_project_config, load_config
from .config.panels import PanelCache, load_panels
from .config.schema import RutherfordConfig
from .domain.error_codes import ErrorCode
from .domain.errors import RutherfordError
from .io.ledger import RunLedger
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
    #: One-time-per-session guard for the first-run setup hint, so the advisory nudges once rather than on
    #: every call (:meth:`persistence_notice`).
    setup_hint_emitted: bool = False

    def new_correlation_id(self) -> str:
        """Mint a short correlation id for a tool call."""
        return uuid.uuid4().hex[:12]

    def persistence_notice(self, *, persisted: bool, complex_run: bool, external_tracking: bool) -> str | None:
        """Advisory F2 notice for a tool result, or ``None`` when there is none.

        Up to two non-fatal hints, joined: a one-time (per session) first-run hint when this workspace has no
        Rutherford config dir, and a suggestion to keep a complex (multi-voice / write) run as a durable job
        when persistence is off by default and the run was not persisted. ``external_tracking`` suppresses the
        suggestion (an orchestrator already tracks the run). stdio cannot prompt, so the notice rides the
        result's ``notice`` field for the calling agent to relay. A single string (not a list) so the TOON
        payload stays decodable.
        """
        notices: list[str] = []
        # Key the first-run hint off a recognized config FILE, not the ``.rutherford`` dir. The dir is created
        # by a persisted run's ledger (``.rutherford/jobs/``) even with no config, so a dir check would wrongly
        # suppress the hint; conversely ``has_project_config`` honors EVERY project config name
        # (``rutherford.toml`` / ``.rutherford.toml`` / ``.rutherford/config.toml``), so a workspace configured
        # via any of them never gets a false "no config" hint.
        if not self.setup_hint_emitted and not has_project_config(Path.cwd()):
            notices.append(
                "No Rutherford config in this workspace yet: runs are ephemeral by default (nothing is kept "
                "on disk). To keep runs as durable jobs under .rutherford/jobs/, set the default for this "
                "workspace (setup scope=project write=true, then default_persistence=job in the file) or pass "
                "persist=true per call."
            )
            self.setup_hint_emitted = True
        if complex_run and not persisted and not external_tracking and self.config.default_persistence == "ephemeral":
            notices.append(
                "This run spans multiple voices or writes. Pass persist=true to keep it as a durable job for "
                "tracking, reference, or to continue later."
            )
        return "\n\n".join(notices) if notices else None


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
    # The durable run ledger (F2): the one writer of the jobs directory, shared by the delegation primitive
    # (leaf records) and the panels (parent records). ``jobs_dir`` defaults to ``<cwd>/.rutherford/jobs`` so
    # kept runs live with the project. The directory is created lazily on the first persisted write, so an
    # all-ephemeral workspace never grows a ``.rutherford/jobs`` tree.
    ledger = RunLedger(_resolve_jobs_dir(resolved_config))
    delegation = DelegationService(resolved_descriptors, resolved_config, cooldown=cooldown, ledger=ledger)
    consensus = ConsensusService(delegation, resolved_descriptors, resolved_config, cooldown=cooldown, ledger=ledger)
    debate = DebateService(resolved_descriptors, resolved_config, delegation, ledger=ledger)
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


def _resolve_jobs_dir(config: RutherfordConfig) -> Path:
    """The durable-jobs root (F2): the configured ``jobs_dir``, else ``<cwd>/.rutherford/jobs``.

    A configured path is taken as-is (absolute or resolved relative to the cwd); when unset, jobs live under
    the project's own ``.rutherford/`` dir -- with the project, not the user's home -- matching where config
    and panels live.
    """
    if config.jobs_dir:
        return Path(config.jobs_dir).expanduser()
    return Path.cwd() / ".rutherford" / "jobs"
