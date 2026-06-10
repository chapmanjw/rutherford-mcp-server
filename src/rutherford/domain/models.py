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
    Strategy,
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
    #: A non-negative voting weight for the ``weighted`` strategy; ``None`` means the default 1.0. A
    #: negative weight is rejected -- it would shrink the strategy denominator and let one voice
    #: manufacture a "majority".
    weight: float | None = Field(default=None, ge=0)
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


class Provenance(BaseModel):
    """Which provider / model / CLI build actually produced a voice's answer.

    The effective-identity record, distinct from the requested :class:`Target`: a consensus panel of
    N CLIs can quietly be one model in N costumes (the roster is largely bring-your-own-model), and
    this is what makes that falsifiable. Every field is optional and defaults to ``None`` so the whole
    block is dropped from the wire (``exclude_none``) until something is known -- it degrades to
    "unknown" rather than guessing.

    * ``provider`` -- the model's vendor: ``anthropic`` / ``openai`` / ``google`` / ``alibaba`` /
      ``meta`` / ``local`` / etc., or ``None`` when undetermined. A serving platform (AWS, Azure, a
      gateway) is NOT a vendor -- it goes on ``backend``.
    * ``backend`` -- the serving platform when it differs from the vendor's own API
      (``bedrock`` / ``vertex`` / ``aws`` / ``azure`` / ``openrouter`` / ``groq`` / ...); ``None`` for
      a direct vendor call or a local model. Kept separate from ``provider`` so "the same model served
      two ways" is not counted as two providers.
    * ``model`` -- the resolved requested model (the model the CLI was asked to use; for a CLI whose
      selector namespaces the vendor, e.g. OpenCode, the bare model id). Rutherford does not currently
      read a CLI-reported resolved id back from the output, so this may be an alias (``opus``) rather
      than a pinned snapshot id -- which is why two CLIs naming the same model differently read as two
      distinct models (see :class:`DiversityReport`).
    * ``cli_version`` -- the CLI build that produced the answer, for drift forensics.
    * ``confirmed`` -- ``True`` when provider/model came from a definitive signal (the model string the
      CLI was given and uses, an explicit backend flag, a fixed-vendor CLI); ``False`` when inferred by
      a fallible heuristic (a model-name pattern, a home-vendor default).
    """

    provider: str | None = None
    backend: str | None = None
    model: str | None = None
    cli_version: str | None = None
    confirmed: bool = False


class DiversityReport(BaseModel):
    """How much genuine model/provider diversity a panel's answers actually had.

    Computed from each answering voice's :class:`Provenance`. ``distinct_models`` is the headline
    ("5 voices -> 3 distinct models"): a panel that is one model in several CLI costumes is made
    visible instead of passing as N independent opinions. Voices whose model could not be resolved are
    counted in ``unknown`` and excluded from the distinct tallies (not assumed same or different).
    ``low_diversity`` flags when, among the resolved voices, the distinct *model* count OR the distinct
    *provider* (vendor) count collapses below the configured ``min_distinct`` floor -- the vendor axis
    catches the case the model axis misses (see below).

    The distinct-model count is identity-string level: without a model registry, ``opus`` via one CLI
    and ``claude-opus-4`` via another read as two models, so model-string diversity can over-report
    independence. The provider axis is the backstop -- those two still share the vendor ``anthropic``,
    so a same-vendor panel is flagged even when the model strings differ. It is a labeled heuristic --
    a strictly better signal than the zero a pre-F3 panel gave -- not a proof of independence.
    """

    answered_voices: int
    distinct_models: int
    distinct_providers: int
    unknown: int
    low_diversity: bool
    models: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)


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
    #: Extra CLI args the service resolved from ``[adapters.<id>] extra_args`` for this target.
    #: An adapter that supports passthrough flags (e.g. Ollama, LM Studio) appends these to its argv.
    extra_args: list[str] = Field(default_factory=list)


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
    #: An ordered list of alternate targets to try when the primary delegation fails on a retryable
    #: category (rate-limit, auth, timeout, this CLI being down, ...) -- a cross-CLI fallback chain
    #: (F7). Empty means no chain. Each alternate is tried in order until one answers; a benched
    #: (cooled-down) alternate is skipped. The model fallback above runs first (same adapter); this is
    #: the cross-target layer.
    fallback: list[Target] = Field(default_factory=list)


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
    #: When a cross-target fallback chain fired (F7), the display labels of the targets that failed
    #: before the one that answered, in order. ``target`` then holds the target that actually answered.
    #: ``None`` means no cross-target fallback happened.
    fallback_chain: list[str] | None = None
    #: The effective provider/model/CLI-version that actually answered (F3). ``None`` when none could
    #: be determined, so the field is absent from the wire rather than reported as a guess.
    provenance: Provenance | None = None


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
    #: How to aggregate the voices. ``all-voices`` (default) returns every voice unchanged; any
    #: other strategy asks each voice for a verdict and returns a :class:`StrategyResult`.
    strategy: Strategy = Strategy.ALL_VOICES
    #: When set, each voice is asked to return JSON matching this schema (including a ``verdict``
    #: field); without it, verdicts are read from a final ``VERDICT: <token>`` line.
    verdict_schema: dict[str, Any] | None = None
    #: An optional target to write the synthesis (used only when ``synthesize`` is on). Defaults to the
    #: first successful voice when unset; pass a distinct CLI so an independent, non-participant judge
    #: combines the panel instead of one of the debaters.
    judge: Target | None = None


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
    #: The display label of the target that wrote the synthesis (a judge if one was named, else the
    #: first successful voice), so a reader can see whether the synthesizer was a panel participant.
    synthesis_by: str | None = None
    skipped: list[SkippedTarget] = Field(default_factory=list)
    #: Effective model/provider diversity across the answering voices (F3). ``None`` when no voice
    #: answered (nothing to measure).
    diversity: DiversityReport | None = None


# --- Consensus strategies ----------------------------------------------------


class VoiceVerdict(BaseModel):
    """One voice's verdict in a strategy run: the seat, its extracted verdict, and its full answer.

    ``verdict`` is ``None`` when the voice failed or its answer could not be parsed into a verdict
    (``unparseable``); such a voice is still returned but excluded from the aggregation. ``text`` is
    the voice's full answer, kept so the reader can see the reasoning behind the verdict.
    """

    label: str
    cli: str
    model: str | None = None
    #: Non-negative voting weight for the ``weighted`` strategy; a negative weight is rejected so a
    #: voice cannot shrink the denominator and manufacture a majority.
    weight: float = Field(default=1.0, ge=0)
    parity: bool = False
    ok: bool = True
    verdict: str | None = None
    #: Why this voice has no verdict, so an excluded voice is never silent: ``failed`` (the voice
    #: errored), ``unparseable`` (it answered but no verdict could be extracted), or ``None`` when a
    #: verdict was extracted. Lets a reader tell a mis-parse from an abstention.
    no_verdict_reason: str | None = None
    text: str = ""
    #: The effective provider/model that answered this seat (F3), carried from the voice's result so a
    #: strategy reader sees per-voice identity. ``None`` when undetermined.
    provenance: Provenance | None = None


class StrategyResult(BaseModel):
    """The aggregated outcome of a consensus strategy, plus every voice's verdict.

    ``outcome`` is the category the strategy reached (``unanimous`` | ``majority`` | ``no_majority`` |
    ``plurality`` | ``tied`` | ``split`` | ``agree`` | ``escalate`` | ``no_quorum``). ``decision`` is
    the winning verdict token when one was reached, else ``None``. The legacy :class:`ConsensusResult`
    shape is what a caller still gets when no strategy (or ``all-voices``) is used; this richer shape
    appears only when they opt in.
    """

    strategy: Strategy
    outcome: str
    decision: str | None = None
    voices: list[VoiceVerdict] = Field(default_factory=list)
    skipped: list[SkippedTarget] = Field(default_factory=list)
    #: Effective model/provider diversity across the answering voices (F3). ``None`` when none answered.
    diversity: DiversityReport | None = None


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
    #: An optional target to write the closing synthesis. Defaults to the first surviving voice when
    #: unset; pass a distinct CLI for an independent, non-participant judge.
    judge: Target | None = None


class DebateContribution(BaseModel):
    """One voice's turn in a single debate round.

    Carries the answer plus the metadata needed to retrace who said what and under which steering:
    the voice ``label``, its resolved ``target`` (with any model fallback already applied), the
    ``round_index`` it belongs to, and the optional ``stance`` / ``role`` it argued under. A failed
    turn is recorded with ``ok=false`` and an ``error`` rather than dropped, so the transcript shows
    where a voice fell out.
    """

    label: str
    #: A unique internal key for this seat, distinct from the human-facing ``label``. Survival and
    #: own-position lookup key on this so two seats that share a ``(cli, model)`` (and thus a label)
    #: never merge into one. Empty only for contributions built outside the debate service.
    seat_id: str = ""
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
    #: The effective provider/model that produced this turn (F3), carried from the voice's result.
    #: ``None`` when undetermined.
    provenance: Provenance | None = None


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
    #: The display label of the target that wrote the closing synthesis (a judge if named, else the
    #: first surviving voice).
    synthesis_by: str | None = None
    skipped: list[SkippedTarget] = Field(default_factory=list)
    #: Effective model/provider diversity across the final round's answering voices (F3). ``None``
    #: when no voice survived to the final round.
    diversity: DiversityReport | None = None


# --- Jobs --------------------------------------------------------------------


class Job(BaseModel):
    """A background execution with an id, status, and eventual result."""

    id: str
    kind: str
    status: JobStatus = JobStatus.PENDING
    result: DelegationResult | ConsensusResult | DebateResult | StrategyResult | None = None
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
    #: True for an adapter you only need if you opt in (e.g. a local model). ``doctor`` frames an
    #: absent or not-ready optional adapter as "only if you want it", never as something to fix.
    optional: bool = False
    path: str | None = None
    version: str | None = None
    auth: AuthStatus
    models: list[str] = Field(default_factory=list)
    #: The configured default model (`[adapters.<id>] default_model`), so a reader can see which model
    #: a no-model delegation will use. ``None`` when no default is configured for this adapter.
    default_model: str | None = None
    capabilities: AdapterCapabilities
    runtime: Runtime = Runtime.NATIVE
    notes: list[str] = Field(default_factory=list)
