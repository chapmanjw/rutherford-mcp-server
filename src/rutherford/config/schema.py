# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The validated configuration schema.

A global config file plus a project-local override that merges over it (see
:mod:`rutherford.config.loader`). Covers enabled adapters, per-adapter default models, the
default safety mode and timeout, role directories, the recursion and fan-out guards, and the
trusted workspace allowlist. Invalid config raises
:class:`~rutherford.domain.errors.ConfigError`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.enums import SafetyMode

_log = logging.getLogger(__name__)


class AdapterConfig(BaseModel):
    """Per-adapter overrides applied to a built-in adapter."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    enabled: bool = True
    default_model: str | None = None
    #: Per-adapter run timeout in seconds. Overrides the global ``default_timeout_s`` for this
    #: adapter when a call names no ``timeout_s``; ``None`` falls back to the global default. Useful
    #: for a slow local model (e.g. Ollama on a CPU, or LM Studio's JIT model load) whose cold load
    #: can exceed the global budget.
    timeout_s: float | None = Field(default=None, gt=0)
    #: Extra command-line arguments appended verbatim to the adapter's invocation. Honored by the
    #: local-model adapters -- Ollama (e.g. ``["--keepalive", "30s"]``) and LM Studio (e.g.
    #: ``["--ttl", "3600"]``).
    extra_args: list[str] = Field(default_factory=list)


class RutherfordConfig(BaseModel):
    """The full validated configuration."""

    model_config = ConfigDict(extra="forbid")

    #: Restrict the registry to these adapter ids; ``None`` enables every known adapter.
    enabled_adapters: list[str] | None = None
    #: Per-adapter overrides keyed by adapter id.
    adapters: dict[str, AdapterConfig] = Field(default_factory=dict)
    #: Default safety posture when a caller does not specify one.
    default_safety_mode: SafetyMode = SafetyMode.READ_ONLY
    #: Default per-run timeout in seconds.
    default_timeout_s: float = Field(default=300.0, gt=0)
    #: Extra directories to search for role markdown files (built-in roles always load).
    role_dirs: list[str] = Field(default_factory=list)
    #: Maximum delegation depth before a chain is refused.
    max_depth: int = Field(default=3, ge=1, le=10)
    #: Maximum number of targets a single consensus call may fan out to.
    max_targets: int = Field(default=8, ge=1, le=32)
    #: Maximum number of rounds a single debate call may run (each round is a full panel pass).
    max_debate_rounds: int = Field(default=4, ge=1, le=10)
    #: Minimum number of parseable voices (ok, with an extracted verdict) an aggregating consensus
    #: strategy needs before it will return a decision; below it the outcome is ``no_quorum``. Guards
    #: against certifying an outcome off one surviving voice when the rest failed.
    min_quorum: int = Field(default=1, ge=1)
    #: The distinct-identity floor below which a consensus/debate panel's answers are flagged
    #: ``low_diversity`` (F3): when at least two voices resolve but they collapse to fewer than this
    #: many distinct *models* OR distinct *providers* (vendors), the panel was less independent than
    #: its CLI count implied. Default 2 (two same-model or same-vendor voices flag); raise it to demand
    #: wider diversity.
    min_distinct: int = Field(default=2, ge=1)
    #: Maximum CLI subprocess delegations Rutherford runs at once, across every panel (a global
    #: semaphore in the delegation primitive). Decouples panel width from host process pressure: a
    #: wide consensus or a multi-round debate cannot launch more than this many heavy agent
    #: subprocesses simultaneously. When not set explicitly it defaults to ``max_targets`` (see the
    #: validator below), so raising ``max_targets`` does not silently throttle a single auto-panel;
    #: set it explicitly to pin a different cap (e.g. lower on a laptop). Read once at startup.
    max_concurrency: int = Field(default=8, ge=1)
    #: Cooldown (F7): how many *unhealthy* failures (down / throttled / mis-launching -- not a bad
    #: prompt) an adapter may have within ``cooldown_window_s`` before it is benched for
    #: ``cooldown_duration_s``. A benched adapter is left out of an auto-expanded (``expand_all``) panel
    #: and skipped as a fallback candidate, but an explicit delegation to it still runs. Set to ``0`` to
    #: disable cooldown entirely. In-memory and process-global; resets on restart.
    cooldown_threshold: int = Field(default=3, ge=0)
    #: The sliding window over which ``cooldown_threshold`` failures are counted.
    cooldown_window_s: float = Field(default=120.0, gt=0)
    #: How long a benched adapter stays benched before it is tried again.
    cooldown_duration_s: float = Field(default=60.0, gt=0)
    #: Absolute paths under which write/yolo delegations are permitted.
    trusted_workspaces: list[str] = Field(default_factory=list)
    #: Whether consensus synthesizes server-side by default (off by default per the spec).
    synthesize_default: bool = False
    #: Opt-in: after a *successful* ``read_only`` or ``propose`` delegation whose working directory is
    #: a git repo, fingerprint the tree under ``working_dir`` before and after (status with
    #: ``--ignored=matching`` plus the unstaged and staged diffs, scoped to that subtree) and fail the
    #: result with ``READONLY_VIOLATED`` if it changed -- making the safety promise a checked
    #: invariant. Off by default (it adds git calls per delegation). What it catches and does not: it
    #: detects a further edit to an already-dirty file (content, not just status) and a write to a
    #: gitignored path, and scopes to ``working_dir`` so unrelated changes elsewhere in the repo are
    #: not mis-attributed; but a write *outside* the repo is unobservable, and under concurrent fan-out
    #: on a *shared* tree a peer's write can still be attributed here (soundest for a single
    #: delegation -- worktree isolation gives per-voice soundness). Checked only when the run itself
    #: succeeded, so a real failure (timeout, non-zero exit, drift) is never masked.
    verify_read_only: bool = False
    #: Seconds to cache an adapter's metadata probe (``detect`` / ``check_auth`` / ``available_models``)
    #: so ``capabilities`` / ``doctor`` / ``expand_all`` do not re-fork the same ``--version`` / status
    #: subprocesses within one burst. ``0`` disables caching. ``doctor``'s live check invalidates first.
    probe_cache_ttl_s: float = Field(default=10.0, ge=0)
    #: Hard per-probe timeout ceiling (seconds): a metadata probe is capped at ``min(its own, this)``
    #: so a hung probe cannot stall ``capabilities`` / ``doctor`` / ``expand_all`` forever. The default
    #: (20s) is sized to the longest *legitimate* adapter probe -- ``codex doctor --json`` asks for 20s
    #: and a slow auth/version check for 15s -- so the ceiling acts only as a hang guard and never
    #: shortens a probe an adapter deliberately budgeted (an 8s default did, mis-reporting a slow but
    #: valid Bedrock/custom-provider auth as logged-out). Lower it only if you accept that risk.
    probe_timeout_s: float = Field(default=20.0, ge=1)
    #: Seconds a finished background job is retained before eviction.
    job_ttl_s: float = Field(default=3600.0, ge=1)
    #: Maximum number of background jobs retained at once; creating one past the cap (after evicting
    #: expired jobs) fails with ``TOO_MANY_JOBS``.
    max_jobs: int = Field(default=100, ge=1)
    #: Structured-log verbosity (stderr, JSON lines). ``debug`` | ``info`` | ``warning`` | ``error``.
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    #: Structured-log format: ``json`` (one JSON object per line, to stderr) or ``off`` to silence it.
    #: stdout is the MCP channel and is never written to.
    log_format: Literal["json", "off"] = "json"

    @model_validator(mode="after")
    def _default_concurrency_to_targets(self) -> RutherfordConfig:
        """Default ``max_concurrency`` to ``max_targets`` when it was not set explicitly.

        Keeps the documented coupling honest: an operator who raises ``max_targets`` (in a config file
        or via ``RUTHERFORD_MAX_TARGETS``) gets a matching concurrency cap, so a single auto-panel is
        not silently throttled to the old default; an explicit ``max_concurrency`` still wins (e.g. a
        lower cap on a laptop). Runs at load time; the semaphore is built once at startup.
        """
        if "max_concurrency" not in self.model_fields_set:
            self.max_concurrency = self.max_targets
        return self

    @model_validator(mode="after")
    def _resolve_dirs(self) -> RutherfordConfig:
        """Resolve ``trusted_workspaces`` / ``role_dirs`` to absolute paths and warn on a missing one.

        Resolving makes the trusted-workspace match reliable regardless of the process cwd. A
        configured directory that does not exist is logged as a warning (it would otherwise fail
        silently -- a typo'd trust path that never matches, or a role dir whose files never load) but
        is not a hard error: an MCP stdio server should still start, and a missing trust path fails
        safe (the write is denied), so a warning is the right balance over bricking startup.
        """
        self.trusted_workspaces = [_resolve_dir("trusted_workspaces", p) for p in self.trusted_workspaces]
        self.role_dirs = [_resolve_dir("role_dirs", p) for p in self.role_dirs]
        return self

    def default_model_for(self, adapter_id: str) -> str | None:
        """Return the configured default model for ``adapter_id``, if any."""
        entry = self.adapters.get(adapter_id)
        return entry.default_model if entry is not None else None

    def timeout_for(self, adapter_id: str) -> float | None:
        """Return the configured per-adapter timeout (seconds) for ``adapter_id``, if any."""
        entry = self.adapters.get(adapter_id)
        return entry.timeout_s if entry is not None else None

    def extra_args_for(self, adapter_id: str) -> list[str]:
        """Return the configured extra CLI args for ``adapter_id`` (empty when none)."""
        entry = self.adapters.get(adapter_id)
        return list(entry.extra_args) if entry is not None else []


def _resolve_dir(field: str, raw: str) -> str:
    """Resolve ``raw`` to an absolute path, warning (not failing) if the directory does not exist."""
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError:
        _log.warning("config %s: could not resolve path %r; leaving it as-is", field, raw)
        return raw
    if not resolved.is_dir():
        _log.warning("config %s: directory does not exist: %s", field, resolved)
    return str(resolved)
