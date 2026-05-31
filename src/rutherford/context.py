# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The result-envelope helpers and the per-call tool context.

Mirrors the owner's ``toolSuccess`` / ``toolError`` pair: one helper to build a success payload
and one to build an error payload, so every tool returns an identically shaped, TOON-encoded
result. The :class:`AppContext` holds the long-lived services built once at startup; the
:class:`ToolContext` carries the per-call correlation id, timeout, and cancellation signal.

These helpers return strings (the TOON text a FastMCP tool returns as a text block). Whether an
error payload is returned normally or raised as an MCP error is the thin tool layer's decision,
which keeps this module independent of the transport.
"""

from __future__ import annotations

from typing import Any

from .domain.error_codes import ErrorCode
from .domain.errors import RutherfordError
from .io.serialize import encode


def tool_success(data: Any) -> str:
    """Build a success payload: ``data`` serialized through the TOON seam."""
    return encode(data)


def tool_error(code: ErrorCode | str, message: str, details: dict[str, Any] | None = None) -> str:
    """Build an error payload carrying a stable error code, serialized through the TOON seam."""
    error: dict[str, Any] = {"code": str(code), "message": message}
    if details:
        error["details"] = details
    return encode({"error": error})


def error_payload_from(exc: RutherfordError) -> str:
    """Build an error payload from a :class:`RutherfordError`."""
    return tool_error(exc.code, exc.message, exc.details)
