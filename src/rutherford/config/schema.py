# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The validated configuration schema.

A global config file plus a project-local override that merges over it (see
:mod:`rutherford.config.loader`). Covers enabled adapters, per-adapter default models, the
default safety mode and timeout, role directories, the recursion and fan-out guards, the trusted
workspace allowlist, and config-defined generic adapters. Invalid config raises
:class:`~rutherford.domain.errors.ConfigError`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.enums import OutputMode, Runtime, SafetyMode


class GenericSafetyConfig(BaseModel):
    """Per-SafetyMode argv fragments for a config-driven generic adapter."""

    model_config = ConfigDict(extra="forbid")

    read_only: list[str] = Field(default_factory=list)
    propose: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    yolo: list[str] = Field(default_factory=list)


class GenericAdapterConfig(BaseModel):
    """Defines a generic adapter entirely from config -- no code.

    Suitable for a well-behaved CLI with a clean headless invocation. The argv is assembled as
    ``[binary, *base_args, *safety_args, *model_args, *working_dir_args, *extra_args]`` plus the
    prompt (as the final positional argument, or on stdin). Never a shell string.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str
    display_name: str
    binary: str
    base_args: list[str] = Field(default_factory=list)
    prompt_on_stdin: bool = False
    model_flag: str | None = None
    working_dir_flag: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    output_mode: OutputMode = OutputMode.TEXT
    json_text_path: str | None = None
    safety: GenericSafetyConfig = Field(default_factory=GenericSafetyConfig)
    version_args: list[str] = Field(default_factory=lambda: ["--version"])
    static_models: list[str] = Field(default_factory=list)
    auth_env: list[str] = Field(default_factory=list)
    runtime: Runtime = Runtime.NATIVE


class AdapterConfig(BaseModel):
    """Per-adapter overrides applied to a built-in or generic adapter."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    enabled: bool = True
    default_model: str | None = None
    #: Per-adapter run timeout in seconds. Overrides the global ``default_timeout_s`` for this
    #: adapter when a call names no ``timeout_s``; ``None`` falls back to the global default. Useful
    #: for a slow local model (e.g. Ollama on a CPU, or LM Studio's JIT model load) whose cold load
    #: can exceed the global budget.
    timeout_s: float | None = None
    #: Extra command-line arguments appended verbatim to the adapter's invocation. Honored by the
    #: local-model adapters -- Ollama (e.g. ``["--keepalive", "30s"]``) and LM Studio (e.g.
    #: ``["--ttl", "3600"]``); generic adapters carry their own ``extra_args`` in
    #: :class:`GenericAdapterConfig`.
    extra_args: list[str] = Field(default_factory=list)


class RutherfordConfig(BaseModel):
    """The full validated configuration."""

    model_config = ConfigDict(extra="forbid")

    #: Restrict the registry to these adapter ids; ``None`` enables every known adapter.
    enabled_adapters: list[str] | None = None
    #: Per-adapter overrides keyed by adapter id.
    adapters: dict[str, AdapterConfig] = Field(default_factory=dict)
    #: Config-defined generic adapters.
    generic_adapters: list[GenericAdapterConfig] = Field(default_factory=list)
    #: Default safety posture when a caller does not specify one.
    default_safety_mode: SafetyMode = SafetyMode.READ_ONLY
    #: Default per-run timeout in seconds.
    default_timeout_s: float = 300.0
    #: Extra directories to search for role markdown files (built-in roles always load).
    role_dirs: list[str] = Field(default_factory=list)
    #: Maximum delegation depth before a chain is refused.
    max_depth: int = 3
    #: Maximum number of targets a single consensus call may fan out to.
    max_targets: int = 8
    #: Maximum number of rounds a single debate call may run (each round is a full panel pass).
    max_debate_rounds: int = 4
    #: Minimum number of parseable voices (ok, with an extracted verdict) an aggregating consensus
    #: strategy needs before it will return a decision; below it the outcome is ``no_quorum``. Guards
    #: against certifying an outcome off one surviving voice when the rest failed.
    min_quorum: int = Field(default=1, ge=1)
    #: Maximum CLI subprocess delegations Rutherford runs at once, across every panel (a global
    #: semaphore in the delegation primitive). Decouples panel width from host process pressure: a
    #: wide consensus or a multi-round debate cannot launch more than this many heavy agent
    #: subprocesses simultaneously. When not set explicitly it defaults to ``max_targets`` (see the
    #: validator below), so raising ``max_targets`` does not silently throttle a single auto-panel;
    #: set it explicitly to pin a different cap (e.g. lower on a laptop). Read once at startup.
    max_concurrency: int = Field(default=8, ge=1)
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
