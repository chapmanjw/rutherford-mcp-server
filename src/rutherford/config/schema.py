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

from ..domain.enums import Effort, SafetyMode
from ..domain.models import OnBudget

_log = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    """Per-agent configuration: override a built-in agent, or define a brand-new ACP agent.

    Under ACP an agent is just how to launch it as an ACP server plus a few quirks -- there is no
    hand-written per-CLI parser -- so an agent can be declared entirely in config. An id that matches a
    built-in agent overrides its fields (or removes it with ``enabled = false``); an id that does NOT
    match a built-in defines a new agent and must supply ``command``. The launch fields mirror the
    Zed/Cline ``acp.json`` shape, so an imported ``agent_servers`` block lands here directly.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    enabled: bool = True
    #: The launch argv for this agent's ACP server (e.g. ``["codex-acp"]`` or ``["node", "./agent.js"]``).
    #: Required to DEFINE a new agent; for a built-in agent it replaces the default launch command.
    command: list[str] | None = None
    #: Environment variables SET for the agent subprocess, layered on top of the inherited environment
    #: (so the agent's own credential discovery still works). Populated from an acp.json ``env`` block.
    env: dict[str, str] = Field(default_factory=dict)
    #: The fixed model vendor when known (e.g. ``"openai"``), recorded as provenance; ``None`` keeps the
    #: built-in value (or stays unknown for a new agent).
    provider: str | None = None
    default_model: str | None = None
    #: Seconds for the initialize + new_session handshake before it is judged failed; ``None`` keeps the
    #: built-in value (or the 30s default for a new agent). Raise it for a heavyweight agent.
    handshake_timeout_s: float | None = Field(default=None, gt=0)
    #: Per-agent run timeout in seconds. Overrides the global ``default_timeout_s`` for this agent when a
    #: call names no ``timeout_s``; ``None`` falls back to the global default.
    timeout_s: float | None = Field(default=None, gt=0)
    #: Extra arguments appended to the launch argv (after ``command``). Lets a built-in agent gain a flag
    #: without restating its whole command.
    extra_args: list[str] = Field(default_factory=list)
    #: Per-agent default reasoning-effort tier (F8a). Used when a call names no ``effort``; ``None``
    #: falls back to the global ``default_effort``. A no-op for an agent with no effort knob.
    effort: Effort | None = None
    #: The model to retry with when the requested model is unavailable (F7 model fallback). ``None`` (the
    #: default) means this agent exposes no fallback model, so a model-unavailable failure does not retry the
    #: same agent on another model. Set it for an agent that can decline a named model and recover on a
    #: known-good one (most ACP agents cannot, so this stays unset for them).
    fallback_model: str | None = None
    #: Reuse a BUILT-IN agent's launch command under this new id (e.g. ``base = "goose"``). The convenient
    #: way to clone a built-in -- typically paired with ``backend`` to point it at a local model runtime,
    #: or with ``default_model`` to pin a model. Mutually exclusive with ``command``.
    base: str | None = None
    #: Point this agent at a LOCAL model runtime: ``"ollama"`` or ``"lmstudio"``. Rutherford fills in the
    #: right provider env for the ``base`` agent (only ``goose`` is supported as a base today), so a local
    #: model becomes a first-class ACP voice. Requires ``model``; ``base`` defaults to the built-in matching
    #: this id when unset. See ``host`` for a non-default endpoint.
    backend: Literal["ollama", "lmstudio"] | None = None
    #: The model id served by ``backend`` (e.g. ``"gemma3:12b"`` for Ollama, ``"openai/gpt-oss-120b"`` for
    #: LM Studio). Required when ``backend`` is set; becomes the agent's default model.
    model: str | None = None
    #: The ``backend`` endpoint as ``host:port``; defaults to ``localhost:11434`` (Ollama) or
    #: ``localhost:1234`` (LM Studio).
    host: str | None = None

    @model_validator(mode="after")
    def _check_backend(self) -> AgentConfig:
        """A local-backend agent needs a model; ``backend`` and a raw ``command`` are mutually exclusive."""
        if self.backend is not None and not self.model:
            raise ValueError("a local 'backend' agent requires 'model' (the model id the runtime serves)")
        if self.command is not None and (self.base is not None or self.backend is not None):
            raise ValueError("'command' cannot be combined with 'base'/'backend' (choose a raw command OR a base)")
        return self


class RutherfordConfig(BaseModel):
    """The full validated configuration."""

    model_config = ConfigDict(extra="forbid")

    #: Restrict the registry to these agent ids; ``None`` enables every known + configured agent.
    enabled_agents: list[str] | None = None
    #: Agent definitions and overrides keyed by agent id (built-in overrides and new agents alike).
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    #: Zero-config local models: when ``True`` (the default), probe a running Ollama (``:11434``) and
    #: LM Studio (``:1234``) at registry-build time and register each tool-capable model as a
    #: ``goose``-based ACP agent automatically (id ``ollama-<model>`` / ``lmstudio-<model>``). A
    #: built-in or explicit ``[agents.<id>]`` of the same id always wins; a backend that is down is
    #: silently skipped and never breaks startup. Set ``False`` to require explicit local-agent config.
    auto_detect_local_models: bool = True
    #: Default safety posture when a caller does not specify one.
    default_safety_mode: SafetyMode = SafetyMode.READ_ONLY
    #: Default per-run timeout in seconds.
    default_timeout_s: float = Field(default=300.0, gt=0)
    #: Default reasoning-effort tier when a call names none (F8a, 2-L); ``None`` = let the CLI decide.
    default_effort: Effort | None = None
    #: Default wall-clock time budget (seconds) for a panel / job when a call names none (F8a, 2-A').
    #: ``None`` = no budget (the out-of-the-box behavior; a panel runs to completion).
    default_time_budget_s: float | None = Field(default=None, gt=0)
    #: Default disposition at a time-budget deadline (F8a, 2-M) when a call names none.
    default_on_budget: OnBudget = "harvest"
    #: Extra directories to search for role markdown files (built-in roles always load).
    role_dirs: list[str] = Field(default_factory=list)
    #: Maximum delegation depth before a chain is refused.
    max_depth: int = Field(default=3, ge=1, le=10)
    #: Maximum number of targets a single consensus call may fan out to.
    max_targets: int = Field(default=8, ge=1, le=32)
    #: Optional ADVISORY aggregate-agent ceiling (N1, item 3): when set, a panel whose declared width
    #: exceeds it is flagged (``Topology.over_cap``) and a warning is logged, so runaway fan-out is visible
    #: without being blocked. ``None`` (the default) disables the check. Distinct from ``max_targets`` (the
    #: always-on per-call cap): this is the cross-cutting "how many agents total" budget, observed not
    #: enforced unless ``enforce_agent_cap`` is also set. A floor, since psutil sees only local processes.
    max_agents_advisory: int | None = Field(default=None, ge=2)
    #: Hard-enforce the advisory cap (N1, item 3): when ``True`` and ``max_agents_advisory`` is set, a panel
    #: whose declared width exceeds the cap is REFUSED up front with ``AGENT_CAP_EXCEEDED`` instead of merely
    #: warned. Default ``False`` -- the cap is observe-and-warn out of the box (decision: advisory first).
    enforce_agent_cap: bool = False
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
    #: Ceiling on how many live ACP agent sessions Rutherford runs at once across a panel, to decouple panel
    #: width from host pressure (N1 / reliability). Enforced by an :class:`asyncio.Semaphore` the delegation
    #: primitive holds around each ACP turn and that the consensus budget-harvest and a debate's round turns
    #: also acquire, so a wide panel cannot spawn more than this many agents at once on any path. Defaults to
    #: ``max_targets`` (see the validator below).
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
    #: No-self-approval default (F4a, 4-A): when true, a consensus/debate refuses a synthesis/closing that
    #: would be authored by a panel participant (name a non-participant judge). Off by default -- the per-call
    #: ``require_independent_judge`` is the usual opt-in; this is the workspace-wide default for binding verdicts.
    require_independent_judge: bool = False
    #: Workspace-wide default for the RANK no-silent-dismissal surfacing (F4b, 7-G); the per-call
    #: ``require_dissent`` is the usual opt-in. Off by default.
    require_dissent: bool = False
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
    #: Whether a run is persisted to disk as a durable job by default (F2, Model A: durability is
    #: opt-in). ``ephemeral`` (the default) leaves nothing on disk unless a call passes
    #: ``persist=true``; ``job`` persists every run unless a call passes ``persist=false``. A persisted
    #: run is written under :attr:`jobs_dir` as ``<run_id>/state.json`` (JSON) plus Markdown artifacts.
    default_persistence: Literal["ephemeral", "job"] = "ephemeral"
    #: Where durable jobs are written. ``None`` (the default) means ``<cwd>/.rutherford/jobs`` -- the
    #: workspace the server runs in -- so jobs live with the project, not the user's home. Set an
    #: absolute path to relocate them. Rutherford writes its own job state here regardless of a
    #: delegation's safety mode (this is the server's bookkeeping, not an edit to the user's code).
    jobs_dir: str | None = None
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

    def wants_persist(self, persist: bool | None) -> bool:
        """Resolve whether a run should be kept as a durable job (F2): explicit ``persist`` wins, else
        the configured ``default_persistence`` (Model A: ``ephemeral`` out of the box).

        The single source of truth for the persistence axis, shared by the delegation/consensus/debate
        services and the async-submit envelope so the sync and async paths can never silently diverge.
        """
        return persist if persist is not None else (self.default_persistence == "job")

    def default_model_for(self, agent_id: str) -> str | None:
        """Return the configured default model for ``agent_id``, if any."""
        entry = self.agents.get(agent_id)
        return entry.default_model if entry is not None else None

    def timeout_for(self, agent_id: str) -> float | None:
        """Return the configured per-agent timeout (seconds) for ``agent_id``, if any."""
        entry = self.agents.get(agent_id)
        return entry.timeout_s if entry is not None else None

    def effort_for(self, agent_id: str) -> Effort | None:
        """Resolve the default reasoning-effort tier for ``agent_id`` (F8a): per-agent, else global.

        Used when a call names no ``effort``; ``None`` means "let the CLI decide" (no effort flag).
        """
        entry = self.agents.get(agent_id)
        if entry is not None and entry.effort is not None:
            return entry.effort
        return self.default_effort

    def extra_args_for(self, agent_id: str) -> list[str]:
        """Return the configured extra CLI args for ``agent_id`` (empty when none)."""
        entry = self.agents.get(agent_id)
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
