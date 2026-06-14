# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Domain enumerations.

All use :class:`enum.StrEnum` so a member is also its wire string: it serializes cleanly to
TOON/JSON and a config file can supply the bare lower-case value.
"""

from __future__ import annotations

from enum import StrEnum


class SafetyMode(StrEnum):
    """The universal safety posture for a delegation, mapped per adapter to that CLI's flags.

    Ordered from least to most permissive. ``read_only`` is the default everywhere. ``write``
    and ``yolo`` are explicit opt-in and gated by a trusted-workspace check.
    """

    #: Inspect only; the agent must not modify the workspace.
    READ_ONLY = "read_only"
    #: The agent may propose changes (e.g. a diff) but not apply them.
    PROPOSE = "propose"
    #: The agent may modify the workspace, subject to the CLI's normal approvals.
    WRITE = "write"
    #: The agent may act without approval prompts (the CLI's bypass mode).
    YOLO = "yolo"


def is_mutating(mode: SafetyMode) -> bool:
    """Return whether a safety mode can modify the workspace (``write`` or ``yolo``).

    Read-only and propose are non-mutating, so they need no trusted-workspace check.
    """
    return mode in (SafetyMode.WRITE, SafetyMode.YOLO)


class AuthState(StrEnum):
    """The result of a non-destructive auth probe. A probe never triggers a login."""

    #: A usable credential or session was detected.
    AUTHENTICATED = "authenticated"
    #: The CLI is installed but needs an interactive login that Rutherford will not perform.
    NEEDS_LOGIN = "needs_login"
    #: The CLI expects an API key in the environment and none was found.
    API_KEY_MISSING = "api_key_missing"
    #: Auth state could not be determined without running the CLI.
    UNKNOWN = "unknown"


class Runtime(StrEnum):
    """Where an adapter's binary runs relative to the Rutherford host."""

    #: Same OS as the host; no path translation needed.
    NATIVE = "native"
    #: A Linux binary reached from a Windows host (or vice versa) via WSL interop.
    WSL_INTEROP = "wsl_interop"


class OutputMode(StrEnum):
    """How an adapter captures a CLI's final answer."""

    #: A single JSON object on stdout.
    JSON = "json"
    #: Newline-delimited JSON events on stdout.
    JSONL = "jsonl"
    #: Plain text on stdout.
    TEXT = "text"
    #: stdout is unreliable; the answer is read from a transcript file (the Antigravity case).
    TRANSCRIPT = "transcript"


class DelegationMode(StrEnum):
    """Whether a tool call awaits the result or returns a job id."""

    #: Await the result within the timeout.
    SYNC = "sync"
    #: Return a job id immediately; poll for the result.
    ASYNC = "async"


class JobStatus(StrEnum):
    """The lifecycle state of a background job."""

    #: Accepted, not yet started.
    PENDING = "pending"
    #: Running.
    RUNNING = "running"
    #: Finished; a result is available.
    SUCCEEDED = "succeeded"
    #: Finished with an error; an error result is available.
    FAILED = "failed"
    #: Cancelled by the caller before it finished; its CLI process tree was killed.
    CANCELLED = "cancelled"


class Stance(StrEnum):
    """Optional per-target steering for a consensus panel."""

    #: Argue in favor of the proposition.
    FOR = "for"
    #: Argue against the proposition.
    AGAINST = "against"
    #: No steering (the default).
    NEUTRAL = "neutral"


class Strategy(StrEnum):
    """How a consensus panel's voices are aggregated into an outcome."""

    #: Return every voice with no aggregation (the default, today's behavior).
    ALL_VOICES = "all-voices"
    #: Agree only if EVERY eligible voice weighed in and shares one verdict; a failed or unparseable
    #: voice vetoes unanimity (outcome ``split``).
    UNANIMOUS = "unanimous"
    #: A true majority: one verdict must exceed 50% of all eligible voices (failed/unparseable voices
    #: count in the denominator). No verdict over the bar is ``no_majority``.
    MAJORITY = "majority"
    #: A plurality: the single most-voted verdict wins even below 50% (the pre-1.x ``majority``
    #: behavior); a tie at the top is ``tied``.
    PLURALITY = "plurality"
    #: A true majority by weight: one verdict must exceed 50% of the total eligible weight, else
    #: ``no_majority``.
    WEIGHTED = "weighted"
    #: Compare the proposer's verdict against the parity counterweights; disagreement escalates.
    PARITY_PAIR = "parity-pair"


class Effort(StrEnum):
    """The universal reasoning-effort tier a caller can ask a CLI to spend (F8a, decision 2-L).

    Maps per adapter to that CLI's native knob (``map_effort``), clamped to the nearest tier the CLI
    supports and reported as ``effort_applied``. Ordered least to most. The ``-fast`` serving-latency
    variants are deliberately excluded -- they are orthogonal to thinking depth, not an effort tier.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


#: The canonical effort order, least to most, for clamp-to-nearest in ``map_effort``.
EFFORT_ORDER: tuple[Effort, ...] = (Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.XHIGH)


class ReexecutionSafety(StrEnum):
    """Whether a failed ACP turn may be silently re-issued (transport / model / cross-agent fallback).

    Distinct from "did a side effect happen": after a ``session/prompt`` is accepted a turn can be
    filesystem-clean yet still unsafe to silently re-run, because cost may have accrued and the agent's
    state is ambiguous. Only :attr:`SAFE` may enter a retry/fallback path (the gate replaces a bare
    ``is_retryable`` check). Ordered least to most dangerous.
    """

    #: A pre-prompt failure (spawn / handshake). The request never ran; re-issuing it elsewhere is safe.
    SAFE = "safe"
    #: The prompt was accepted with no observed external side effect, but cost may have accrued and the
    #: agent saw context, so a silent re-run would double-spend.
    DUPLICATE_COST = "duplicate_cost"
    #: It is unknown whether the prompt or a tool call ran (e.g. an ambiguous transport drop).
    AMBIGUOUS = "ambiguous"
    #: A known external side effect occurred (``fs/write`` or a terminal command). Never auto-retry.
    SIDE_EFFECTED = "side_effected"


class ActivityEventKind(StrEnum):
    """What happened in a run's live activity stream (N1, item 3): the kinds of an :class:`ActivityEvent`.

    One structured vocabulary feeding two transparency sinks that never diverge -- the poll view
    (``activity`` tool over running jobs) and the MCP push (``Context.report_progress`` on a sync call).
    A discrete kind lets a consumer filter (push a panel's lifecycle, fold ``voice_finished`` into a
    progress fraction) without re-parsing a free-text line.
    """

    #: A single delegation (a panel voice or a standalone delegate) started running its subprocess.
    VOICE_STARTED = "voice_started"
    #: A single delegation finished -- cleanly, with an error, or cut at a budget (see ``status``).
    VOICE_FINISHED = "voice_finished"
    #: A local descendant-count snapshot from psutil sampling. Carried on ``voice_finished``'s
    #: ``observed_agents`` today (a voice reports its own peak); reserved as a standalone kind for a
    #: future coarse whole-run sampler.
    OBSERVED = "observed"
    #: A consensus/debate panel began, carrying the declared width (the fan-out total).
    PANEL_STARTED = "panel_started"
    #: A panel finished, carrying its outcome summary.
    PANEL_FINISHED = "panel_finished"
    #: A time-budget deadline was reached and the panel is harvesting (one tick at the deadline).
    BUDGET_TICK = "budget_tick"
    #: A voice/turn was cut at the time-budget deadline (its process tree killed).
    CUT = "cut"
    #: The whole run was cancelled by the caller (best-effort, on the outer cancel path).
    JOB_CANCELLED = "job_cancelled"
