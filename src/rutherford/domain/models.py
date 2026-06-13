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
from .error_codes import ErrorCode

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


class ErrorInfo(BaseModel):
    """The error payload carried in a failed result envelope.

    ``code`` is typed as the :class:`~rutherford.domain.error_codes.ErrorCode` enum -- the codes
    are a closed client contract, so a typoed or ad-hoc string fails at construction here rather
    than serializing cleanly into a client-visible envelope. (A valid code STRING still coerces;
    the wire shape is unchanged.)
    """

    code: ErrorCode
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
    #: True when this CLI has no write posture distinct from its permission bypass: ``write`` and
    #: ``yolo`` map to the same bypass flag (e.g. Antigravity print mode, which has no granular
    #: approval -- without the bypass, write-mode edits are silently never applied). Surfaced so a
    #: caller opting into ``write`` can see it is opting into the bypass, and so ``doctor`` says
    #: so out loud.
    write_uses_bypass: bool = False


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
    process environment.
    """

    argv: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
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
    id, and an optional role preamble. ``build_invocation`` is responsible for incorporating
    ``role_preamble`` -- via a native system-prompt flag where the CLI has one, or by prepending
    it to the prompt otherwise -- so the service never double-injects it.
    """

    target: Target
    safety_mode: SafetyMode = SafetyMode.READ_ONLY
    working_dir: str | None = None
    correlation_id: str = ""
    role_preamble: str | None = None
    #: The resume session id for this call, so a transcript-based adapter (Antigravity) can read the
    #: exact conversation it told the CLI to resume rather than re-guessing it. ``None`` for a fresh run.
    session_id: str | None = None
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
    include_raw: bool = False
    #: Persist this run as a durable job under ``<jobs_dir>/<run_id>/`` (Model A: durability is
    #: opt-in; an ephemeral run leaves nothing on disk). ``None`` follows the configured
    #: ``default_persistence``; ``True`` / ``False`` force it for this one call.
    persist: bool | None = None
    #: When set, this delegation is a voice of a persisted panel: its run record is written as a child
    #: of this parent run id (consensus/debate set it on each voice). ``None`` for a top-level run.
    parent_run_id: str | None = None
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
    #: The git working-tree changes under ``working_dir`` captured *after* a mutating (write/yolo) run,
    #: best-effort and only when the run is **persisted** (it feeds the run record). The jobs directory
    #: is excluded. ``None`` when not captured (read-only run, ephemeral run, not a git repo, or git
    #: unavailable). Caveat: in a tree that was already dirty before the run, this reflects the current
    #: worktree state, not strictly this run's delta -- it is "what changed", not a proven attribution.
    changed_files: list[str] | None = None
    #: The directory this run was persisted to when it was run as a durable job
    #: (``<jobs_dir>/<run_id>``). ``None`` for an ephemeral run (Model A: nothing on disk unless asked).
    run_dir: str | None = None
    #: Advisory, non-fatal notices for the caller (e.g. a first-run persistence setup hint, or a
    #: suggestion to keep a complex run as a job). ``None`` when there are none, so the field is absent
    #: from the wire. Never affects the result -- a UX channel, not an error.
    notice: str | None = None


# --- Consensus ---------------------------------------------------------------


class ConsensusRequest(BaseModel):
    """The same prompt asked of several targets in parallel.

    ``stances``, when given, is parallel to ``targets`` and steers each voice. ``synthesize``
    requests an optional server-side synthesis; ``None`` means the caller omitted it and the
    configured ``synthesize_default`` applies (false out of the box), so an explicit ``False``
    always wins over the config. When ``expand_all`` is set, ``targets`` is
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
    synthesize: bool | None = None
    timeout_s: float | None = None
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
    #: Persist this panel as a durable job (F2): a parent record plus a child record per voice, under
    #: ``<jobs_dir>/``. ``None`` follows ``default_persistence``; ``True`` / ``False`` force it.
    persist: bool | None = None
    #: Suppress the suggest-a-job advisory notice when an external orchestrator already tracks this run.
    external_tracking: bool = False


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
    #: Advisory, non-fatal notices for the caller (e.g. a suggestion to keep this panel as a job).
    notice: str | None = None
    #: The directory this panel was persisted to when kept as a job (the parent record, with a child
    #: record per voice). ``None`` for an ephemeral panel.
    run_dir: str | None = None


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
    #: Advisory, non-fatal notices for the caller (e.g. a suggestion to keep this panel as a job).
    notice: str | None = None
    #: The directory this panel was persisted to when kept as a job (parent + a child record per voice).
    #: ``None`` for an ephemeral panel.
    run_dir: str | None = None


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
    include_raw: bool = False
    #: An optional target to write the closing synthesis. Defaults to the first surviving voice when
    #: unset; pass a distinct CLI for an independent, non-participant judge.
    judge: Target | None = None
    #: Persist this debate as a durable job (F2): a parent record plus a child record per voice/round,
    #: under ``<jobs_dir>/``. ``None`` follows ``default_persistence``; ``True`` / ``False`` force it.
    persist: bool | None = None
    #: Suppress the suggest-a-job advisory notice when an external orchestrator already tracks this run.
    external_tracking: bool = False


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
    #: The cost this turn reported, carried from the voice's result, so a persisted debate's parent can
    #: roll up panel cost into ``state.toon`` (decision 1-D). ``None`` when the CLI reported none.
    cost: Cost | None = None
    #: The directory this turn was persisted to when the debate was kept as a job (the child record of
    #: the panel parent). ``None`` for an ephemeral debate.
    run_dir: str | None = None


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
    #: Advisory, non-fatal notices for the caller (e.g. a suggestion to keep this debate as a job).
    notice: str | None = None
    #: The directory this debate was persisted to when kept as a job (parent + a child record per
    #: voice/round). ``None`` for an ephemeral debate.
    run_dir: str | None = None


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


# --- Durable run records (F2) ------------------------------------------------


class Topology(BaseModel):
    """The process/agent fan-out a run declared and (locally) realized -- the F2 record's topology slot.

    Reserved in the schema from day one (decision 1-D) so a kept run has a home for fan-out data the
    moment the N1 topology observation work (the roadmap's item 3) lands, without a schema bump
    invalidating the corpus recorded before then. All fields are optional and unset today: nothing
    populates this yet -- the record carries the slot, not the data. ``declared`` is the intended
    width, ``realized_delegations`` is Rutherford's own calls (incl. fallback / nested), and
    ``observed_peak_agents`` is the local descendant high-water mark (a floor: a CLI's remote agents
    are invisible). ``over_cap`` flags a panel that ran wider than the advisory aggregate cap.
    """

    declared: int | None = None
    realized_delegations: int | None = None
    observed_peak_agents: int | None = None
    over_cap: bool = False


class RunRecord(BaseModel):
    """A durable, replay-complete record of one run, persisted as a job (F2).

    Written to ``<jobs_dir>/<run_id>/state.toon`` when a call opts into persistence -- Model A:
    durability is opt-in, an ephemeral run leaves nothing on disk, so the corpus is the runs you
    chose to keep. Distinct from the in-memory, mutable, TTL-evicted :class:`Job`: a ``RunRecord``
    is an immutable audit/replay entry. ``schema_version`` is pinned from day one so records written
    under one version stay readable as later fields are added (a reader checks it before trusting a
    field's presence).

    Replay-completeness, deliberately bounded: ``argv``, ``cwd``, the ``prompt``, ``role``, ``files``,
    the resolved ``model`` / ``adapter_version`` / ``provenance``, and the outcome are captured so the
    run can be re-issued and audited. Two things are *recomposed*, not stored verbatim: the child
    process ``env`` is **never** persisted (it can carry API keys and other secrets that must not hit
    disk -- replay reconstructs it from config), and a stdin-based adapter's composed stdin payload is
    rebuilt from ``prompt`` + ``role`` + ``files`` rather than captured, so ``argv`` may hold only the
    flags. A reader must recompose, not assume ``argv`` alone is the whole input.
    """

    model_config = ConfigDict(frozen=True)

    #: Bumped when the persisted shape changes; a reader checks it before trusting a field.
    schema_version: int = 1
    run_id: str
    #: ``delegate`` | ``consensus`` | ``debate`` -- which tool produced this run.
    kind: str
    status: JobStatus = JobStatus.SUCCEEDED
    #: Wall-clock epoch seconds (not the monotonic ``duration_s``), so a record has a real timestamp.
    created_at: float = 0.0
    finished_at: float = 0.0
    duration_s: float = 0.0
    #: A parent run's id when this record is a child of a panel (consensus/debate); ``None`` at top level.
    parent_run_id: str | None = None
    #: For a panel record (``kind`` consensus/debate), the run ids of the child voice records so a reader
    #: can reassemble the panel from its parts: one child per voice in panel order for a consensus, and
    #: one child per turn in round-major order (round 1's voices, then round 2's, ...) for a debate. Empty
    #: for a leaf delegate record.
    child_run_ids: list[str] = Field(default_factory=list)
    # --- what produced the answer ---
    cli: str
    #: The model the caller requested (pre-fallback); ``None`` means "the adapter's default". Kept
    #: alongside the resolved ``model`` so the record shows requested-vs-resolved when a model fallback
    #: fired (the ``argv`` is the *resolved* invocation; the requested form recomposes from this +
    #: ``prompt``/``role``/``files``).
    requested_model: str | None = None
    model: str | None = None
    adapter_version: str | None = None
    provenance: Provenance | None = None
    safety_mode: SafetyMode = SafetyMode.READ_ONLY
    #: Process/agent fan-out slot (decision 1-D), reserved for the item-3 N1 work; unset today.
    topology: Topology | None = None
    # --- the pinned invocation (replay-complete; no env -- it can hold secrets) ---
    argv: list[str] = Field(default_factory=list)
    cwd: str | None = None
    # --- inputs (the stdin payload is recomposed from these, not stored) ---
    prompt: str = ""
    #: The role persona this run argued under, part of the recomposable input. ``None`` when unset.
    role: str | None = None
    files: list[str] = Field(default_factory=list)
    # --- outputs ---
    ok: bool = True
    error_code: ErrorCode | None = None
    changed_files: list[str] = Field(default_factory=list)
    cost: Cost | None = None


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
    notes: list[str] = Field(default_factory=list)
