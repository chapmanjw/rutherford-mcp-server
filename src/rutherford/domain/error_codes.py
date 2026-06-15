# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Stable, machine-readable error codes.

These codes are part of Rutherford's public contract: a code is never renamed or repurposed,
only added. MCP clients and skills may switch on them, so they must remain stable across minor
versions. This mirrors the owner's other servers, which centralize their error codes in one
module. The codes are carried in the ``error.code`` field of the normalized result envelope.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """The closed set of stable Rutherford error codes."""

    #: The target CLI's binary is not installed or not on PATH.
    BINARY_NOT_FOUND = "BINARY_NOT_FOUND"
    #: The target CLI is installed but not authenticated, and cannot log in non-interactively.
    #: Reserved (not currently raised): auth failures surface pre-run as panel skip reasons or
    #: mid-run as AUTH_FAILED, but the code stays in the documented contract.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: A write or yolo delegation targeted a workspace that is not on the trusted allowlist.
    WORKSPACE_NOT_TRUSTED = "WORKSPACE_NOT_TRUSTED"
    #: A read_only or propose delegation mutated its git working tree, caught by the optional
    #: post-run verification (``verify_read_only``). The safety promise was not kept by the CLI.
    READONLY_VIOLATED = "READONLY_VIOLATED"
    #: The run exceeded its timeout and its process tree was killed.
    TIMEOUT = "TIMEOUT"
    #: The CLI exited with a non-zero status.
    NONZERO_EXIT = "NONZERO_EXIT"
    #: The requested model is not available to this account/plan (a refinement of a non-zero exit;
    #: the delegation retries once with the adapter's fallback model where one exists).
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    #: The provider rate-limited or quota-exhausted the call -- a transient failure worth retrying
    #: on a different target (a refinement of a non-zero exit).
    RATE_LIMITED = "RATE_LIMITED"
    #: The CLI was authenticated enough to start but the call was rejected for auth (a 401/403,
    #: an expired or invalid credential) -- distinct from BINARY_NOT_FOUND/AUTH_REQUIRED, which are
    #: pre-run. A different target may still answer.
    AUTH_FAILED = "AUTH_FAILED"
    #: The prompt plus context exceeded the model's window. A different target with a larger window
    #: may still answer (a refinement of a non-zero exit).
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    #: The subprocess could not be launched (the binary, a shim, or a runtime failed to start),
    #: distinct from the CLI running and exiting non-zero.
    SPAWN_FAILED = "SPAWN_FAILED"
    #: The CLI's output could not be parsed into a normalized result.
    PARSE_ERROR = "PARSE_ERROR"
    #: The CLI reported success but its output did not match the adapter's expected machine-readable
    #: shape -- a drift canary. The CLI's output format likely changed underneath the adapter, so a
    #: result that would otherwise read as ``ok`` is failed loudly instead of trusted silently.
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    #: A session-resume invocation was rejected by the CLI's argument parser (a Rutherford/CLI
    #: mismatch), distinct from a normal non-zero exit so a lost resume is not silently swallowed.
    RESUME_FAILED = "RESUME_FAILED"
    #: The Antigravity transcript file could not be found or read.
    TRANSCRIPT_NOT_FOUND = "TRANSCRIPT_NOT_FOUND"
    #: A request or argument failed validation.
    INVALID_INPUT = "INVALID_INPUT"
    #: The request named a CLI id that is not in the registry.
    UNKNOWN_TARGET = "UNKNOWN_TARGET"
    #: The delegation chain reached the configured maximum depth.
    MAX_DEPTH_EXCEEDED = "MAX_DEPTH_EXCEEDED"
    #: A consensus call requested more targets than the per-request cap allows.
    TOO_MANY_TARGETS = "TOO_MANY_TARGETS"
    #: A panel's declared width exceeded the advisory aggregate-agent cap AND hard enforcement was on
    #: (``enforce_agent_cap``). Off by default: the cap is advisory (observed and warned, not refused), so
    #: this is raised only when an operator opts into preemptive refusal (N1, item 3). Distinct from
    #: TOO_MANY_TARGETS, which is the always-on per-call fan-out cap.
    AGENT_CAP_EXCEEDED = "AGENT_CAP_EXCEEDED"
    #: A referenced background job id does not exist (or its result has expired).
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    #: A background job could not be created because the configured ``max_jobs`` cap is reached.
    TOO_MANY_JOBS = "TOO_MANY_JOBS"
    #: A named role could not be found in any configured role directory.
    ROLE_NOT_FOUND = "ROLE_NOT_FOUND"
    #: A ``role="<id>"`` on ``delegate`` / ``consensus`` / ``debate`` named a persona that the
    #: ``RoleStore`` does not know (no built-in and no ``role_dirs`` file of that id). The error lists
    #: the known role ids; ``list_roles`` enumerates them.
    UNKNOWN_ROLE = "UNKNOWN_ROLE"
    #: A named panel could not be found in any discovered panels file.
    PANEL_NOT_FOUND = "PANEL_NOT_FOUND"
    #: A panels file failed to parse or validate (bad TOON, unknown CLI, malformed target).
    PANEL_INVALID = "PANEL_INVALID"
    #: A time-budgeted run hit its deadline with ZERO usable results (below ``min_quorum``) -- the
    #: zero-yield edge of a harvest (F8a, decision 2-E'). A harvest that yields at least ``min_quorum``
    #: usable voices is a SUCCESS (``ok=true`` + ``stop_reason="budget"``), not this code; this is
    #: reserved for the genuine empty harvest. Not retryable and not cooldown-counting (it is a budget
    #: outcome, not an unhealthy adapter).
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: ACP transport: the agent subprocess could not be launched (binary missing, exec error). Pre-prompt,
    #: so the failure is re-execution-safe (a different agent may answer).
    ACP_SPAWN_FAILED = "ACP_SPAWN_FAILED"
    #: ACP transport: the initialize/new_session handshake failed (protocol, auth, or version). Pre-prompt,
    #: so re-execution-safe.
    ACP_HANDSHAKE_FAILED = "ACP_HANDSHAKE_FAILED"
    #: An ACP prompt turn exceeded its timeout; its session was cancelled and any streamed partial is
    #: preserved. Post-prompt, so NOT re-execution-safe.
    ACP_TURN_TIMEOUT = "ACP_TURN_TIMEOUT"
    #: The agent ended the turn by refusing (``stopReason`` refusal). Post-prompt; not re-execution-safe.
    ACP_REFUSED = "ACP_REFUSED"
    #: The agent ended the turn cleanly but produced no answer text. Post-prompt; not re-execution-safe.
    ACP_EMPTY_ANSWER = "ACP_EMPTY_ANSWER"
    #: An error surfaced from the ACP connection after the prompt was accepted (a transport drop, a protocol
    #: error mid-turn). Ambiguous, so NOT re-execution-safe by default.
    ACP_TURN_ERROR = "ACP_TURN_ERROR"
    #: An unexpected internal error.
    INTERNAL = "INTERNAL"
