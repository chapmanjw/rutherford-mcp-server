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
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: The requested SafetyMode is not supported by the target adapter.
    UNSUPPORTED_SAFETY_MODE = "UNSUPPORTED_SAFETY_MODE"
    #: A write or yolo delegation targeted a workspace that is not on the trusted allowlist.
    WORKSPACE_NOT_TRUSTED = "WORKSPACE_NOT_TRUSTED"
    #: The run exceeded its timeout and its process tree was killed.
    TIMEOUT = "TIMEOUT"
    #: The CLI exited with a non-zero status.
    NONZERO_EXIT = "NONZERO_EXIT"
    #: The CLI's output could not be parsed into a normalized result.
    PARSE_ERROR = "PARSE_ERROR"
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
    #: A referenced background job id does not exist (or its result has expired).
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    #: A named role could not be found in any configured role directory.
    ROLE_NOT_FOUND = "ROLE_NOT_FOUND"
    #: A named panel could not be found in any discovered panels file.
    PANEL_NOT_FOUND = "PANEL_NOT_FOUND"
    #: A panels file failed to parse or validate (bad TOON, unknown CLI, malformed target).
    PANEL_INVALID = "PANEL_INVALID"
    #: An unexpected internal error.
    INTERNAL = "INTERNAL"


#: All known error codes, for membership tests.
ALL_ERROR_CODES: frozenset[str] = frozenset(code.value for code in ErrorCode)


def is_error_code(value: str) -> bool:
    """Return whether ``value`` is a known :class:`ErrorCode`."""
    return value in ALL_ERROR_CODES
