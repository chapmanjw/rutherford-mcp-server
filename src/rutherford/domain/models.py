# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Domain models.

Pydantic models for every value that crosses a layer boundary: the unit of delegation
(:class:`Target`), the normalized request and result envelopes, the adapter value objects
(:class:`InvocationSpec`, :class:`ProcessResult`, and friends), and the consensus and job types.
Adapters and services exchange these; nothing passes around raw CLI-specific shapes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AuthState,
    DelegationMode,
    JobStatus,
    OutputMode,
    Runtime,
    SafetyMode,
    Stance,
)

# --- The unit of delegation --------------------------------------------------


class Target(BaseModel):
    """A ``(cli, model)`` pair: the unit of delegation.

    The CLI alone is never the unit. Bring-your-own-model CLIs (OpenCode, Goose) expose many
    models through one adapter, and the same adapter may appear several times in a consensus
    panel with different models. ``model`` is ``None`` to mean the adapter's default model.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    cli: str
    model: str | None = None


# --- Small shared value objects ----------------------------------------------


class Cost(BaseModel):
    """Cost reported by a CLI, where available. All fields optional."""

    usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class Artifact(BaseModel):
    """A file or diff the delegated agent produced or changed."""

    path: str
    kind: str = "file"
    summary: str | None = None


class ErrorInfo(BaseModel):
    """The error payload carried in a failed result envelope."""

    code: str
    message: str
    details: dict[str, Any] | None = None


# --- Adapter value objects ---------------------------------------------------


class DetectResult(BaseModel):
    """Whether a CLI's binary is installed and runnable."""

    installed: bool
    path: str | None = None
    version: str | None = None


class AuthStatus(BaseModel):
    """The result of a non-destructive auth probe."""

    state: AuthState
    detail: str | None = None


class AdapterCapabilities(BaseModel):
    """Feature flags an adapter advertises, so the core can adapt behavior generically."""

    supports_resume: bool = False
    supports_model_selection: bool = False
    supports_working_dir: bool = False
    supports_file_context: bool = False
    supports_list_models: bool = False
    supports_system_prompt: bool = False
    output_mode: OutputMode = OutputMode.TEXT
    file_context_style: str | None = None
    runtime: Runtime = Runtime.NATIVE


class SafetyFlags(BaseModel):
    """A SafetyMode translated to one CLI's approval/sandbox flags.

    ``args`` are appended to the argv; ``env`` is overlaid on the child environment. ``note``
    documents the mapping for ``doctor`` and logs. An adapter's ``map_safety`` must return a
    value for every :class:`~rutherford.domain.enums.SafetyMode` and default conservatively.
    """

    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class InvocationSpec(BaseModel):
    """A fully resolved, ready-to-run subprocess invocation.

    Produced by ``CLIAdapter.build_invocation`` and consumed by ``ProcessRunner.run``. ``argv``
    is always a list; a shell string is never built. ``env`` is overlaid on the inherited
    process environment. ``runtime`` tells the runner whether path translation is needed.
    """

    argv: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    runtime: Runtime = Runtime.NATIVE
    stdin: str | None = None


class ProcessResult(BaseModel):
    """The raw outcome of running a subprocess."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False


class InvocationContext(BaseModel):
    """Context handed to ``build_invocation`` and ``parse_output``.

    Carries the resolved target, the chosen safety mode, the working directory, a correlation
    id, the current delegation depth, an optional role preamble, and (for transcript-based
    adapters) a hint at where the transcript will land. ``build_invocation`` is responsible for
    incorporating ``role_preamble`` -- via a native system-prompt flag where the CLI has one,
    or by prepending it to the prompt otherwise -- so the service never double-injects it.
    """

    target: Target
    safety_mode: SafetyMode = SafetyMode.READ_ONLY
    working_dir: str | None = None
    correlation_id: str = ""
    depth: int = 0
    role_preamble: str | None = None
    transcript_dir: str | None = None


# --- Delegation request / result --------------------------------------------


class DelegationRequest(BaseModel):
    """A normalized delegation, independent of any CLI."""

    target: Target
    prompt: str
    working_dir: str | None = None
    files: list[str] = Field(default_factory=list)
    role: str | None = None
    safety_mode: SafetyMode = SafetyMode.READ_ONLY
    mode: DelegationMode = DelegationMode.SYNC
    timeout_s: float | None = None
    session_id: str | None = None
    depth: int = 0
    include_raw: bool = False


class DelegationResult(BaseModel):
    """The normalized result envelope every adapter must produce.

    The same shape regardless of the CLI's native output format. ``text`` is the clean final
    answer; ``raw`` is the unparsed stdout/stderr, included only when the caller asks for it.
    ``session_id`` is opaque and round-trips to the CLI's own resume mechanism.
    """

    target: Target
    ok: bool
    exit_code: int | None = None
    text: str = ""
    raw: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    duration_s: float = 0.0
    session_id: str | None = None
    cost: Cost | None = None
    error: ErrorInfo | None = None
    safety_mode: SafetyMode = SafetyMode.READ_ONLY


# --- Consensus ---------------------------------------------------------------


class ConsensusRequest(BaseModel):
    """The same prompt asked of several targets in parallel.

    ``stances``, when given, is parallel to ``targets`` and steers each voice. ``synthesize``
    requests an optional server-side synthesis; it is off by default, so every voice is
    returned for the orchestrator to synthesize.
    """

    targets: list[Target]
    prompt: str
    stances: list[Stance] | None = None
    working_dir: str | None = None
    files: list[str] = Field(default_factory=list)
    role: str | None = None
    safety_mode: SafetyMode = SafetyMode.READ_ONLY
    synthesize: bool = False
    timeout_s: float | None = None
    depth: int = 0
    include_raw: bool = False


class ConsensusResult(BaseModel):
    """Every voice from a consensus panel, plus an optional synthesis."""

    voices: list[DelegationResult]
    synthesis: str | None = None


# --- Jobs --------------------------------------------------------------------


class Job(BaseModel):
    """A background execution with an id, status, and eventual result."""

    id: str
    kind: str
    status: JobStatus = JobStatus.PENDING
    result: DelegationResult | ConsensusResult | None = None
    error: ErrorInfo | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    progress: list[str] = Field(default_factory=list)


# --- Health / capability reporting ------------------------------------------


class AdapterStatus(BaseModel):
    """A per-adapter health and capability snapshot, used by ``capabilities`` and ``doctor``."""

    id: str
    display_name: str
    installed: bool
    path: str | None = None
    version: str | None = None
    auth: AuthStatus
    models: list[str] = Field(default_factory=list)
    capabilities: AdapterCapabilities
    runtime: Runtime = Runtime.NATIVE
    notes: list[str] = Field(default_factory=list)
