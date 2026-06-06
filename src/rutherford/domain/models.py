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
    """A delegation target: a ``(cli, model)`` pair plus optional per-seat metadata.

    The CLI alone is never the unit. Bring-your-own-model CLIs (OpenCode, Goose) expose many
    models through one adapter, and the same adapter may appear several times in a consensus
    panel with different models. ``model`` is ``None`` to mean the adapter's default model.

    The metadata fields are all optional and default to ``None`` so a bare ``(cli, model)`` target
    is unchanged on the wire: ``role`` overrides the tool-level role for this seat, ``label`` is the
    key the seat appears under in a result, ``weight`` and ``parity`` feed the consensus strategies,
    and ``stance`` steers the seat (taking precedence over a parallel stances list).
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    cli: str
    model: str | None = None
    role: str | None = None
    label: str | None = None
    weight: float | None = None
    parity: bool | None = None
    stance: Stance | None = None

    @property
    def display_label(self) -> str:
        """The key this seat appears under: an explicit ``label``, else ``cli:model`` (or ``cli``)."""
        if self.label:
            return self.label
        return f"{self.cli}:{self.model}" if self.model else self.cli

    @property
    def effective_weight(self) -> float:
        """The weight used by weighted strategies; ``1.0`` when unset."""
        return 1.0 if self.weight is None else self.weight

    @property
    def is_parity(self) -> bool:
        """Whether this seat is a parity counterweight for the parity-pair strategy."""
        return bool(self.parity)


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
    #: Per-call confirmation that a write/yolo delegation may mutate ``working_dir`` even when
    #: it is not on the configured trusted-workspace allowlist.
    trust_workspace: bool = False
    #: When the requested model is unavailable, retry once with the adapter's fallback model.
    allow_model_fallback: bool = True


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
    #: When the originally requested model was unavailable and the delegation fell back to the
    #: adapter's fallback model, this records the model that was requested. ``target.model`` then
    #: holds the model that actually answered. ``None`` means no fallback happened.
    fallback_from: str | None = None


# --- Consensus ---------------------------------------------------------------


class ConsensusRequest(BaseModel):
    """The same prompt asked of several targets in parallel.

    ``stances``, when given, is parallel to ``targets`` and steers each voice. ``synthesize``
    requests an optional server-side synthesis; it is off by default, so every voice is
    returned for the orchestrator to synthesize. When ``expand_all`` is set, ``targets`` is
    ignored and the panel is every installed + authenticated adapter (capped at ``max_targets``),
    each at its default model.
    """

    targets: list[Target] = Field(default_factory=list)
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
    #: Build the panel from every installed + authenticated adapter instead of ``targets``.
    expand_all: bool = False


class SkippedTarget(BaseModel):
    """An adapter left out of an auto-expanded panel, with the reason."""

    cli: str
    reason: str


class ConsensusResult(BaseModel):
    """Every voice from a consensus panel, plus an optional synthesis.

    ``skipped`` lists adapters that were considered for an auto-expanded (``expand_all``) panel but
    left out -- not installed, not authenticated, or over the per-call target cap -- so the caller
    can see the full panel that was attempted.
    """

    voices: list[DelegationResult]
    synthesis: str | None = None
    skipped: list[SkippedTarget] = Field(default_factory=list)


# --- Debate ------------------------------------------------------------------


class DebateRequest(BaseModel):
    """A multi-round debate across several targets, with a retraceable transcript.

    Round 1 collects each voice's independent answer. Each later round shows a voice the other
    voices' latest positions and asks it to critique and revise its own, so the panel actually
    argues instead of answering in isolation. ``rounds`` is the number of rounds to run (at least
    one, capped by ``max_debate_rounds``); the debate stops early if fewer than two voices remain.
    ``synthesize`` (on by default here) adds a closing pass that states where the panel converged
    and where it still disagrees. ``stances``, when given, is parallel to ``targets`` and a voice
    keeps its assigned stance through every round.
    """

    targets: list[Target] = Field(default_factory=list)
    prompt: str
    rounds: int = 2
    stances: list[Stance] | None = None
    working_dir: str | None = None
    files: list[str] = Field(default_factory=list)
    role: str | None = None
    safety_mode: SafetyMode = SafetyMode.READ_ONLY
    synthesize: bool = True
    timeout_s: float | None = None
    depth: int = 0
    include_raw: bool = False


class DebateContribution(BaseModel):
    """One voice's turn in a single debate round.

    Carries the answer plus the metadata needed to retrace who said what and under which steering:
    the voice ``label``, its resolved ``target`` (with any model fallback already applied), the
    ``round_index`` it belongs to, and the optional ``stance`` / ``role`` it argued under. A failed
    turn is recorded with ``ok=false`` and an ``error`` rather than dropped, so the transcript shows
    where a voice fell out.
    """

    label: str
    target: Target
    round_index: int
    stance: Stance | None = None
    role: str | None = None
    ok: bool
    text: str = ""
    raw: str | None = None
    duration_s: float = 0.0
    error: ErrorInfo | None = None
    fallback_from: str | None = None


class DebateRound(BaseModel):
    """Every participating voice's contribution for one round of a debate, in panel order."""

    index: int
    contributions: list[DebateContribution] = Field(default_factory=list)


class DebateResult(BaseModel):
    """The full, retraceable transcript of a multi-round debate, plus an optional closing pass.

    ``rounds`` holds every voice's answer at every round it took part in, so a reader can follow how
    the positions shifted and where they converged or split -- this is the "thinking out loud" the
    transcript exists to preserve. ``final`` is the closing synthesis when ``synthesize`` was set.
    ``skipped`` mirrors the consensus field for any target left out before the debate began.
    """

    prompt: str
    rounds: list[DebateRound] = Field(default_factory=list)
    final: str | None = None
    skipped: list[SkippedTarget] = Field(default_factory=list)


# --- Jobs --------------------------------------------------------------------


class Job(BaseModel):
    """A background execution with an id, status, and eventual result."""

    id: str
    kind: str
    status: JobStatus = JobStatus.PENDING
    result: DelegationResult | ConsensusResult | DebateResult | None = None
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
