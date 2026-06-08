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

from pydantic import BaseModel, ConfigDict, Field

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
    #: subprocesses simultaneously. Defaults to ``max_targets`` so a single auto-panel is unchanged;
    #: raise on a big box, lower on a laptop.
    max_concurrency: int = Field(default=8, ge=1)
    #: Absolute paths under which write/yolo delegations are permitted.
    trusted_workspaces: list[str] = Field(default_factory=list)
    #: Whether consensus synthesizes server-side by default (off by default per the spec).
    synthesize_default: bool = False
    #: Opt-in: after a ``read_only`` or ``propose`` delegation whose working directory is a git repo,
    #: compare ``git status`` before and after and fail the result with ``READONLY_VIOLATED`` if the
    #: tree changed -- turning the safety promise into a checked invariant. Off by default: it adds a
    #: git call per delegation, and under concurrent fan-out on a *shared* tree one voice's mutation
    #: is mis-attributed to its peers, so it is soundest for a single delegation (worktree isolation
    #: gives per-voice soundness). Only git working directories are checked.
    verify_read_only: bool = False

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
