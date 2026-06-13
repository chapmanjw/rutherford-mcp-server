# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared builders for the normalized result envelope.

Every adapter's ``parse_output`` must return a :class:`DelegationResult`, including on timeout
and non-zero exit. These helpers build the common shapes -- success, timeout, non-zero exit, and
parse failure -- so each adapter writes only the CLI-specific extraction and the envelope stays
identical across adapters.
"""

from __future__ import annotations

import re

from ..domain.error_codes import ErrorCode
from ..domain.models import (
    Cost,
    DelegationResult,
    ErrorInfo,
    InvocationContext,
    ProcessResult,
)

# CSI (colors, cursor moves) and OSC (window-title) escape sequences emitted by CLIs that print
# to a terminal. Stripped from text-mode answers so the normalized text is clean.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output."""
    return _ANSI_RE.sub("", text)


def success_result(
    ctx: InvocationContext,
    raw: ProcessResult,
    text: str,
    *,
    session_id: str | None = None,
    cost: Cost | None = None,
) -> DelegationResult:
    """Build a successful result with the extracted final text."""
    return DelegationResult(
        target=ctx.target,
        ok=True,
        exit_code=raw.exit_code,
        text=text,
        duration_s=raw.duration_s,
        session_id=session_id,
        cost=cost,
        safety_mode=ctx.safety_mode,
    )


def error_result(
    ctx: InvocationContext,
    raw: ProcessResult | None,
    code: ErrorCode,
    message: str,
    *,
    text: str = "",
    details: dict[str, object] | None = None,
    partial: str | None = None,
    session_id: str | None = None,
) -> DelegationResult:
    """Build a failed result carrying a stable error code.

    ``partial`` preserves the stdout the child wrote before it was cut, when the caller wants it kept on
    the envelope (the timeout path passes it). It is never the answer ``text`` -- only a preserved trace
    of in-flight work (F8a, 2-F: capture always, never surface a fault's bytes as a candidate answer).
    ``session_id`` is carried even on a failure when the output established a resumable session before it
    failed -- so a cut voice whose partial held a session but no answer yet can still be resumed (F8a, 2-I).
    """
    return DelegationResult(
        target=ctx.target,
        ok=False,
        exit_code=raw.exit_code if raw is not None else None,
        text=text,
        duration_s=raw.duration_s if raw is not None else 0.0,
        error=ErrorInfo(code=code, message=message, details=details),
        safety_mode=ctx.safety_mode,
        partial=partial,
        session_id=session_id,
    )


def timeout_result(ctx: InvocationContext, raw: ProcessResult) -> DelegationResult:
    """Build the result for a run that exceeded its timeout, preserving any pre-deadline stdout.

    A single delegation is the degenerate time-budget case (F8a, 2-behavior): there is no panel to
    harvest across, so a timeout "collapses toward timeout" but still keeps the partial stdout the CLI
    streamed before the deadline on ``partial`` (captured by the runner in :attr:`ProcessResult.partial`),
    rather than discarding the work. It stays a ``TIMEOUT`` fault -- the partial is a preserved trace, not
    the answer.
    """
    return error_result(
        ctx,
        raw,
        ErrorCode.TIMEOUT,
        f"{ctx.target.cli} timed out",
        partial=raw.partial,
    )


def nonzero_result(ctx: InvocationContext, raw: ProcessResult, text: str = "") -> DelegationResult:
    """Build the result for a non-zero exit, surfacing stderr (or the given text)."""
    message = (raw.stderr.strip() or text or f"{ctx.target.cli} exited with code {raw.exit_code}")[:2000]
    return error_result(ctx, raw, ErrorCode.NONZERO_EXIT, message, text=text)
