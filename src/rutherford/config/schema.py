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
    #: Absolute paths under which write/yolo delegations are permitted.
    trusted_workspaces: list[str] = Field(default_factory=list)
    #: Whether consensus synthesizes server-side by default (off by default per the spec).
    synthesize_default: bool = False

    def default_model_for(self, adapter_id: str) -> str | None:
        """Return the configured default model for ``adapter_id``, if any."""
        entry = self.adapters.get(adapter_id)
        return entry.default_model if entry is not None else None
