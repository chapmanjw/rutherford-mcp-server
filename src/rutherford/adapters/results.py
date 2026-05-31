# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared builders for the normalized result envelope.

Every adapter's ``parse_output`` must return a :class:`DelegationResult`, including on timeout
and non-zero exit. These helpers build the common shapes -- success, timeout, non-zero exit, and
parse failure -- so each adapter writes only the CLI-specific extraction and the envelope stays
identical across adapters.
"""

from __future__ import annotations

from ..domain.error_codes import ErrorCode
from ..domain.models import (
    Artifact,
    Cost,
    DelegationResult,
    ErrorInfo,
    InvocationContext,
    ProcessResult,
)


def success_result(
    ctx: InvocationContext,
    raw: ProcessResult,
    text: str,
    *,
    session_id: str | None = None,
    cost: Cost | None = None,
    artifacts: list[Artifact] | None = None,
) -> DelegationResult:
    """Build a successful result with the extracted final text."""
    return DelegationResult(
        target=ctx.target,
        ok=True,
        exit_code=raw.exit_code,
        text=text,
        artifacts=artifacts or [],
        duration_s=raw.duration_s,
        session_id=session_id,
        cost=cost,
        safety_mode=ctx.safety_mode,
    )


def error_result(
    ctx: InvocationContext,
    raw: ProcessResult | None,
    code: ErrorCode | str,
    message: str,
    *,
    text: str = "",
    details: dict[str, object] | None = None,
) -> DelegationResult:
    """Build a failed result carrying a stable error code."""
    return DelegationResult(
        target=ctx.target,
        ok=False,
        exit_code=raw.exit_code if raw is not None else None,
        text=text,
        duration_s=raw.duration_s if raw is not None else 0.0,
        error=ErrorInfo(code=str(code), message=message, details=details),
        safety_mode=ctx.safety_mode,
    )


def timeout_result(ctx: InvocationContext, raw: ProcessResult) -> DelegationResult:
    """Build the result for a run that exceeded its timeout."""
    return error_result(
        ctx,
        raw,
        ErrorCode.TIMEOUT,
        f"{ctx.target.cli} timed out",
    )


def nonzero_result(ctx: InvocationContext, raw: ProcessResult, text: str = "") -> DelegationResult:
    """Build the result for a non-zero exit, surfacing stderr (or the given text)."""
    message = (raw.stderr.strip() or text or f"{ctx.target.cli} exited with code {raw.exit_code}")[:2000]
    return error_result(ctx, raw, ErrorCode.NONZERO_EXIT, message, text=text)
