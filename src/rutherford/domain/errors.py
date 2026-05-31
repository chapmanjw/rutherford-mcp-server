# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Typed exceptions.

``RutherfordError`` carries a stable :class:`~rutherford.domain.error_codes.ErrorCode` so a
service can fail with a code that the tool layer turns directly into the error envelope. Graceful
outcomes (missing binary, failed auth, timeout, non-zero exit) are returned as a
``DelegationResult`` with an error, not raised; exceptions are reserved for programmer and
configuration errors and for guard violations the tool layer maps to an envelope.
"""

from __future__ import annotations

from typing import Any

from .error_codes import ErrorCode


class RutherfordError(Exception):
    """Base Rutherford error carrying a stable error code and optional structured details."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: str = str(code)
        self.message = message
        self.details = details


class ConfigError(RutherfordError):
    """Raised when configuration is missing or invalid.

    Fatal: it surfaces at startup, before the server begins serving, and the process exits
    non-zero. Mirrors the typed ``ConfigError`` in the owner's other servers.
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.INVALID_INPUT, message, details=details)


class RegistryError(RutherfordError):
    """Raised when the adapter registry is asked for an unknown id, or is misconfigured.

    The registry is a closed mapping that fails fast rather than silently misclassifying, the
    way the owner's ``ToolCategory`` domain map raises on an unknown domain.
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.UNKNOWN_TARGET, message, details=details)


class DepthLimitError(RutherfordError):
    """Raised when a delegation would exceed the configured maximum depth."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.MAX_DEPTH_EXCEEDED, message, details=details)
