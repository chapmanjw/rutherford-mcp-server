# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Domain models.

Pydantic models for every value that crosses a layer boundary: the unit of delegation
(:class:`Target`), the normalized request and result envelopes, the adapter value objects
(:class:`InvocationSpec`, :class:`ProcessResult`, and friends), and the consensus and job types.
Adapters and services exchange these; nothing passes around raw CLI-specific shapes.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .enums import (
    ActivityEventKind,
    AuthState,
    DelegationMode,
    Effort,
    JobStatus,
    OutputMode,
    Runtime,
    SafetyMode,
    Stance,
    Strategy,
)
from .error_codes import ErrorCode

#: The dispositions for a unit of work that hits its time budget (F8a, decision 2-M):
#: * ``harvest`` (default) -- take the voices that finished, cancel the rest, aggregate over the harvest.
#: * ``continue`` -- the budget is advisory: every voice/round runs to completion (nothing is cut). The
#:   decision's richer "detach: return best-effort now and append the stragglers' late results to the job"
#:   refinement needs job-result mutation and is deferred with the jobs/continuation work (item 9); today
#:   ``continue`` means "run everything," which for an async job completes once all voices finish.
#: * ``resume`` -- cancel the stragglers like ``harvest``, intending a later deliberate come-back to them.
#:   That come-back rides the item-9 continuation primitive (decision 9-F); and a voice cut mid-run has no
#:   cleanly-established session to record passively (its process is killed before ``parse_output`` runs),
#:   so today ``resume`` is equivalent to ``harvest``.
OnBudget = Literal["harvest", "continue", "resume"]

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


class RunRollup(BaseModel):
    """Per-run budget / effort rollup (F8a), the sibling of :class:`DiversityReport`.

    Set on a panel result (and the run record) when a **time budget** was in effect, so a reader can see
    how the unit of work concluded: how many voices were issued, how many answered before the deadline,
    how many were cut for the budget, how many are usable, whether quorum held, and the effort actually
    applied. ``None`` when no budget governed the run.
    """

    stop_reason: str = "ok"  #: "ok" (finished within budget) | "budget" (harvested at the deadline)
    requested: int = 0  #: voices / contributions issued
    answered: int = 0  #: finished before the deadline
    cut: int = 0  #: in-flight, cut at the time-budget deadline (reason time_budget)
    usable: int = 0  #: answered with a non-empty, parseable answer
    quorum_met: bool = True  #: usable >= min_quorum
    elapsed_s: float = 0.0
    time_budget_s: float | None = None
    effort_requested: Effort | None = None
    effort_applied: Effort | None = None  #: the highest tier any seat actually applied
    cost: Cost | None = None  #: summed across the answering voices


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def supports_partial_output(self) -> bool:
        """Whether a cut / timed-out run yields a usable PARTIAL ANSWER vs only a trace (F8a, 2-H).

        Derived from ``output_mode``, never hand-set: JSONL/TEXT stream the answer as it is produced, so
        a time-budget harvest can read a partial answer; single-envelope JSON / TRANSCRIPT emit the
        answer once at the end, so a cut yields only a partial trace, not an answer.
        """
        return self.output_mode in (OutputMode.JSONL, OutputMode.TEXT)


class SafetyFlags(BaseModel):
    """A SafetyMode translated to one CLI's approval/sandbox flags.

    ``args`` are appended to the argv; ``env`` is overlaid on the child environment. ``note``
    documents the mapping for ``doctor`` and logs. An adapter's ``map_safety`` must return a
    value for every :class:`~rutherford.domain.enums.SafetyMode` and default conservatively.
    """

    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class EffortFlags(BaseModel):
    """An :class:`~rutherford.domain.enums.Effort` tier translated to one CLI's flags (F8a, 2-L-map).

    Mirrors :class:`SafetyFlags` (``args`` appended to argv, ``env`` overlaid) plus ``applied``: the tier
    the CLI will actually use after clamping to its supported range, or ``None`` for an adapter with no
    effort knob (a no-op). ``note`` records the mapping -- including a no-op or a clamp (e.g. xhigh -> high)
    -- for ``doctor`` and logs, so a budget that silently did nothing is never silent.
    """

    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    note: str = ""
    applied: Effort | None = None


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
    #: stdout accumulated up to a timeout/cancel, when the process did not finish (F8a, decision 2-F).
    #: The full ``stdout`` field is only populated on a clean finish; ``partial`` preserves the bytes the
    #: child wrote before a deadline cut it, so a time-budget harvest can surface a candidate answer
    #: instead of discarding the work. ``None`` on a clean finish (the answer is in ``stdout``).
    partial: str | None = None
    #: Peak count of local processes (this subprocess plus its recursive descendants) observed by psutil
    #: sampling while the run was live (N1, item 3). A floor, not a ceiling: it sees only LOCAL processes,
    #: so a CLI's remote/cloud agents are invisible. ``None`` when sampling did not run (psutil missing, a
    #: fake runner, or the process exited before the first sample).
    observed_peak_agents: int | None = None


class ActivityEvent(BaseModel):
    """One structured event from a run in flight (N1, item 3): the unit of the live activity stream.

    Single source of truth for live transparency: a service emits these at lifecycle milestones (a voice
    starting/finishing, a panel starting/finishing, a budget cut), and a sync tool projects each one to an
    MCP progress notification (``Context.report_progress``) so the caller sees the work as it happens.
    Distinct from the job-progress STRING channel (``job.progress``), which stays the poll view; this is the
    richer carrier the push side reads counts and status from. Every field but ``kind`` is optional so a
    producer fills only what it knows; ``ts`` is wall-clock epoch seconds, stamped at construction.
    """

    kind: ActivityEventKind
    #: The owning background job id, or ``None`` for a synchronous in-flight call (which has no job record;
    #: its liveness rides the MCP push, not the ``activity`` poll table).
    job_id: str | None = None
    #: A STABLE per-voice identity (the delegation's correlation id), constant across this voice's
    #: ``voice_started`` / ``voice_finished`` / ``cut`` events even when a model fallback changes ``model``
    #: mid-run. The ``activity`` table collapses a voice to one current row by this, not by ``(cli, model,
    #: role)`` -- which a fallback would split into a stale "started" row plus the terminal row. ``None`` on
    #: panel-level events (they are not voice rows).
    correlation_id: str | None = None
    #: The orchestration tool: ``delegate`` | ``consensus`` | ``debate``. ``None`` on a voice event, whose
    #: ``cli`` already identifies it (the parent panel event carries the tool).
    tool: str | None = None
    cli: str | None = None  #: The adapter id of a voice event.
    model: str | None = None  #: The resolved model (post-fallback) of a voice event.
    role: str | None = None  #: The role persona this seat argued under, when set.
    #: A transient lifecycle status for a voice/panel: ``started`` | ``ok`` | ``failed`` | ``cut``.
    status: str | None = None
    elapsed_s: float | None = None  #: Seconds the finished voice/turn ran.
    #: The voice's peak LOCAL descendant count from psutil (a floor; remote agents invisible). ``None``
    #: when not sampled.
    observed_agents: int | None = None
    budget_left_s: float | None = None  #: Remaining time budget at a ``budget_tick``, or ``None``.
    declared: int | None = None  #: A panel's declared width (the fan-out total) on a ``panel_started``.
    done: int | None = None  #: Voices finished so far, for a progress fraction (``done`` of ``declared``).
    depth: int | None = None  #: Delegation depth (``RUTHERFORD_DEPTH``) of this work.
    message: str = ""  #: A human-readable one-line summary, used as the push notification message.
    ts: float = Field(default_factory=time.time)  #: Wall-clock epoch seconds, stamped at construction.


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
    #: The resolved reasoning-effort tier for this call (F8a, 2-L), or ``None`` when none was requested.
    #: The service applies ``map_effort`` generically; an adapter whose effort is encoded in the model id
    #: (Cursor) reads this in ``build_invocation`` to rewrite the model.
    effort: Effort | None = None


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
    #: The reasoning-effort tier to ask the CLI to spend (F8a, decision 2-L), the producer "how much may
    #: it think" knob -- distinct from ``timeout_s`` (the unresponsiveness fault). Mapped per adapter to
    #: its native flag (``map_effort``), clamped to the nearest supported tier, no-op + reported where
    #: unsupported. ``None`` follows the configured ``default_effort``.
    effort: Effort | None = None
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
    #: stdout the CLI wrote before it was cut, preserved instead of discarded (F8a, 2-F). Set when a panel
    #: time-budget deadline harvested this voice in-flight, and also on a single-delegation ``timeout`` (the
    #: degenerate budget case, 2-behavior). A candidate answer for an adapter whose ``supports_partial_output``
    #: is true, a trace otherwise -- but never the answer ``text`` itself. ``None`` when the run finished
    #: cleanly (the answer is in ``text``).
    partial: str | None = None
    #: Why this run stopped, when not a clean finish: ``"budget"`` when a time budget harvested it.
    #: ``None`` on a normal completion. A harvested run is still ``ok`` if it produced an answer (2-E').
    stop_reason: str | None = None
    #: The effort tier requested for this run (echo of the resolved request value), or ``None``.
    effort: Effort | None = None
    #: The effort tier actually applied after the adapter clamped to its supported range (F8a, 2-L-map).
    #: May differ from ``effort`` (e.g. xhigh clamped to high), or be ``None`` when the adapter has no
    #: effort knob (a no-op) or nothing ran.
    effort_applied: Effort | None = None
    #: Peak count of LOCAL processes (this voice plus its descendants) psutil observed while it ran (N1,
    #: item 3), carried up from the :class:`ProcessResult` so a panel can roll it into its
    #: :class:`Topology`. A floor (remote agents invisible). ``None`` when not sampled (nothing ran, a fake
    #: runner, or psutil missing).
    observed_peak_agents: int | None = None
    #: How many subprocess delegations Rutherford launched for this one result, INCLUDING fallback re-runs
    #: (N1, item 3, decision 3-A: realized = own calls incl. fallback): 1 for a clean run, 2 when a model
    #: fallback re-ran, and the primary plus every alternate when a cross-target fallback chain ran. A panel
    #: sums this into :attr:`Topology.realized_delegations`, so a fallback's extra agents are counted. Scope
    #: note: this is the chain aggregate on the RETURNED result (what the panel rolls up); a cross-target
    #: fallback's alternates each persist their OWN leaf record (their own count) and the failed primary is
    #: not persisted, so a standalone ``persist=True`` cross-target fallback's leaf ``state.toon`` records the
    #: winning run rather than the whole chain -- the chain total is on this returned field. A best-effort
    #: count (3-B-limit): a voice cut mid-fallback contributes a floor of 1 (its cancelled task is opaque).
    delegation_call_count: int = 1


class Topology(BaseModel):
    """The process/agent fan-out a run declared and (locally) realized (N1, item 3); the F2 record's slot.

    Reserved in the schema from day one (decision 1-D) so a kept run has a home for fan-out data, and now
    POPULATED by item 3. ``declared`` is the intended width (a panel's target count after filtering; 1 for a
    single delegation). ``realized_delegations`` is Rutherford's own subprocess delegations, INCLUDING
    fallback re-runs (decision 3-A) -- summed across the voices (a consensus) or turns (a debate), where each
    counts its primary plus any model fallback plus the alternates a cross-target fallback chain tried -- so a
    fallback shows up as realized > declared; it excludes the internal synthesis/judge pass. A voice CUT at a
    time-budget deadline contributes a floor of 1 here: its delegate task is cancelled, so an in-flight model
    fallback before the cut is not recovered into the count (consistent with 3-B-limit, realized is best-effort).
    ``observed_peak_agents`` is the local descendant high-water mark from psutil across the voices -- a FLOOR,
    since a CLI's remote agents are invisible, and ``None`` when no voice was sampled. ``over_cap`` flags a run
    whose realized count exceeded the advisory aggregate cap (``max_agents_advisory``); advisory by default, so
    it is informational, not a refusal (the up-front refusal, when ``enforce_agent_cap`` is set, is on the
    declared width).
    """

    declared: int | None = None
    realized_delegations: int | None = None
    observed_peak_agents: int | None = None
    over_cap: bool = False


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
    #: The reasoning-effort tier asked of every voice (F8a, 2-L); ``None`` follows ``default_effort``.
    effort: Effort | None = None
    #: Wall-clock allotment for the WHOLE panel (F8a, 2-A'/2-where), a harvest deadline distinct from each
    #: voice's ``timeout_s`` fault. At the deadline, answered voices are kept and in-flight ones are cut;
    #: the panel aggregates over the harvested set if ``min_quorum`` holds. ``None`` follows the configured
    #: ``default_time_budget_s`` (no budget out of the box).
    time_budget_s: float | None = None
    #: What to do at the deadline (2-M): ``harvest`` (cut the stragglers), ``continue`` (the budget is
    #: advisory -- every voice runs to completion), or ``resume`` (cut the stragglers; today equivalent to
    #: ``harvest`` because a voice cut mid-run has no established session to record -- the deliberate
    #: come-back to a cut voice rides the item-9 continuation primitive). ``None`` follows the configured
    #: ``default_on_budget`` (``harvest`` out of the box), so an omitted value honors the workspace default.
    on_budget: OnBudget | None = None
    #: Active resumable harvest (F8a, 2-I): when ``True``, at the deadline a cut voice whose adapter supports
    #: resume and whose in-flight session was recovered (from its streamed partial) gets a bounded "you're
    #: out of time, give your current best answer" follow-up against that session, and its clean answer
    #: replaces the raw partial. Opt-in because the follow-up itself spends budget you may be out of. A cut
    #: voice with no recoverable session (a single-envelope adapter, or nothing streamed) is unaffected.
    harvest_partial: bool = False


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
    #: ``"budget"`` when a time budget harvested this panel (some voices were cut at the deadline);
    #: ``None`` on a normal completion (F8a, 2-E'). The panel is still ``ok`` if quorum held.
    stop_reason: str | None = None
    #: Per-run budget/effort rollup; set only when a time budget governed the panel (F8a). ``None``
    #: otherwise.
    rollup: RunRollup | None = None
    #: Process/agent fan-out observed for this panel (N1, item 3): declared width, Rutherford's realized
    #: delegations, and the local descendant peak (a floor). ``None`` when not measured.
    topology: Topology | None = None


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
    #: The effort tier this seat actually applied (F8a, 2-L), carried from the voice's result. ``None``
    #: when the adapter has no effort knob or no effort was requested.
    effort_applied: Effort | None = None


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
    #: ``"budget"`` when a time budget harvested this panel; ``None`` on a normal completion (F8a, 2-E').
    stop_reason: str | None = None
    #: Per-run budget/effort rollup; set only when a time budget governed the panel. ``None`` otherwise.
    rollup: RunRollup | None = None
    #: Process/agent fan-out observed for this panel (N1, item 3). ``None`` when not measured.
    topology: Topology | None = None


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
    #: The reasoning-effort tier asked of every voice (F8a, 2-L); ``None`` follows ``default_effort``.
    effort: Effort | None = None
    #: Wall-clock allotment for the WHOLE debate (F8a, 2-A'/2-where/2-behavior). Each round runs under the
    #: REMAINING budget via ``asyncio.wait``: a turn still in flight when the deadline is reached is cut (its
    #: process tree killed), the turns that finished keep their answers, and the transcript-so-far is
    #: finalized with the closing running over what completed. So the budget bounds the debate's real
    #: wall-clock, not just how many whole rounds run. A harvest that leaves fewer than ``min_quorum`` usable
    #: positions in the last round is ``BUDGET_EXHAUSTED`` (2-E'). ``None`` follows ``default_time_budget_s``.
    time_budget_s: float | None = None
    #: What to do at the deadline (2-M): ``harvest`` | ``continue`` (advisory; run every round) | ``resume``
    #: (today equivalent to ``harvest``; the deliberate come-back rides the item-9 primitive). ``None``
    #: follows the configured ``default_on_budget`` (``harvest`` out of the box).
    on_budget: OnBudget | None = None


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
    #: This turn's resume session handle, carried from the voice's result so the debate parent's roster can
    #: record each seat's handle in ``state.toon`` for a later continuation (F8a, 2-I). ``None`` when the
    #: CLI established no resumable session.
    session_id: str | None = None
    #: stdout this turn streamed before a time-budget deadline cut it (F8a, 2-F: capture always). A debate
    #: turn cut mid-flight is a trace, not a stance (a rebuttal assumes each voice saw the others' complete
    #: positions), so the partial is preserved here for the transcript/audit but never promoted to ``text``.
    #: ``None`` when the turn finished cleanly.
    partial: str | None = None
    #: The effective provider/model that produced this turn (F3), carried from the voice's result.
    #: ``None`` when undetermined.
    provenance: Provenance | None = None
    #: The cost this turn reported, carried from the voice's result, so a persisted debate's parent can
    #: roll up panel cost into ``state.toon`` (decision 1-D). ``None`` when the CLI reported none.
    cost: Cost | None = None
    #: The effort tier this turn actually applied (F8a, 2-L), carried from the voice's result. ``None``
    #: when the adapter has no effort knob or no effort was requested.
    effort_applied: Effort | None = None
    #: The local descendant peak psutil observed for this turn (N1, item 3), carried from the voice's result
    #: so the debate parent can roll the panel topology's ``observed_peak_agents`` (a floor). ``None`` when not
    #: sampled (a cut turn, a fake runner, or psutil missing).
    observed_peak_agents: int | None = None
    #: How many subprocess delegations this turn launched, incl. fallback re-runs (N1, item 3, decision 3-A),
    #: carried from the voice's result so the debate parent sums it into ``Topology.realized_delegations``.
    delegation_call_count: int = 1
    #: The files this turn changed (a write-mode debate), carried from the voice's result so the parent
    #: rolls up the panel's changed-file union (decision 1-D), mirroring consensus. Empty for a read run.
    changed_files: list[str] = Field(default_factory=list)
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
    #: ``"budget"`` when a time budget finalized this debate early (at a round boundary); ``None`` on a
    #: normal completion (F8a, 2-E').
    stop_reason: str | None = None
    #: Per-run budget/effort rollup; set only when a time budget governed the debate. ``None`` otherwise.
    rollup: RunRollup | None = None
    #: Process/agent fan-out observed for this debate (N1, item 3). ``None`` when not measured.
    topology: Topology | None = None


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
    #: The structured live-activity buffer (N1, item 3, decision 3-K): the same :class:`ActivityEvent`
    #: stream the sync path pushes, captured here so the ``activity`` tool can render the rich in-flight
    #: table (per-voice cli/model/role/status/observed/budget). ``progress`` stays the human-string
    #: projection of this stream for backward compatibility; the two are two sinks of one source.
    activity: list[ActivityEvent] = Field(default_factory=list)


# --- Durable run records (F2) ------------------------------------------------


class PanelTarget(BaseModel):
    """One seat of a persisted panel's resolved roster: the CLI, its model, stance, and resume handle."""

    cli: str
    model: str | None = None
    stance: Stance | None = None
    #: The seat's resume session handle, recorded in the parent ``state.toon`` so a later continuation can
    #: resume it (F8a, 2-I) -- in particular a voice cut at the time budget, which has no child record of its
    #: own (its handle is recovered from the harvested partial). ``None`` when the seat established no session.
    session_id: str | None = None


class PanelInputs(BaseModel):
    """The resolved orchestration config of a persisted consensus/debate, captured on the panel PARENT
    record so the panel -- not just each voice -- can be replayed or continued from ``state.toon`` alone
    (decision 1-D for the panel parent). Leaf child records capture each voice's per-invocation argv/model;
    these are the panel-level semantics that live on no child: the seat roster, the consensus aggregation
    ``strategy``, whether a ``synthesize`` pass was requested, a debate's ``rounds``, and any ``judge``.
    """

    targets: list[PanelTarget] = Field(default_factory=list)
    strategy: str | None = None
    synthesize: bool | None = None
    rounds: int | None = None
    judge: str | None = None


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
    #: The reasoning-effort tier requested for this run, and the tier actually applied after the adapter
    #: clamped to its supported range (F8a, 2-L). ``None`` when no effort was requested or the adapter
    #: has no knob (a no-op).
    requested_effort: Effort | None = None
    effort_applied: Effort | None = None
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
    #: For a panel PARENT record, the resolved panel orchestration config (seat roster, strategy,
    #: synthesize, rounds, judge) so the panel replays from here; ``None`` for a leaf delegate record.
    panel: PanelInputs | None = None
    # --- outputs ---
    ok: bool = True
    error_code: ErrorCode | None = None
    changed_files: list[str] = Field(default_factory=list)
    cost: Cost | None = None
    #: Why the run stopped when not a clean finish: ``"budget"`` for a time-budget harvest (F8a, 2-E').
    #: ``None`` on a normal completion.
    stop_reason: str | None = None
    #: Per-run budget/effort rollup for a panel parent governed by a time budget (F8a). ``None`` otherwise.
    rollup: RunRollup | None = None


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
