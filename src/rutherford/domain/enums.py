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
