# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared input parsing for the tool layer (ACP-native).

Tools accept simple client-friendly inputs (an agent id, a safety-mode string). These helpers validate and
coerce them into domain types, raising :class:`~rutherford.domain.errors.RutherfordError` with
``INVALID_INPUT`` / ``UNKNOWN_TARGET`` on bad input so the FastMCP layer reports a clean error.
"""

from __future__ import annotations

from ..acp.descriptors import DescriptorRegistry
from ..domain.enums import SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError


def parse_safety_mode(value: str | SafetyMode) -> SafetyMode:
    """Coerce a safety-mode string to :class:`SafetyMode`, or raise ``INVALID_INPUT``."""
    if isinstance(value, SafetyMode):
        return value
    try:
        return SafetyMode(value)
    except ValueError:
        options = ", ".join(mode.value for mode in SafetyMode)
        raise RutherfordError(
            ErrorCode.INVALID_INPUT, f"unknown safety_mode {value!r}; choose one of: {options}"
        ) from None


def resolve_safety_mode(value: str | SafetyMode | None, default: SafetyMode) -> SafetyMode:
    """The effective safety mode for a call: the explicit value, else the configured default.

    ``None`` means the caller omitted the field -- the one case the configured ``default_safety_mode``
    fills -- so an explicit choice always wins over config.
    """
    if value is None:
        return default
    return parse_safety_mode(value)


def ensure_known_agent(descriptors: DescriptorRegistry, agent_id: str) -> None:
    """Raise ``UNKNOWN_TARGET`` if ``agent_id`` is not a registered ACP agent."""
    if not descriptors.has(agent_id):
        known = ", ".join(descriptors.ids()) or "(none)"
        raise RutherfordError(ErrorCode.UNKNOWN_TARGET, f"unknown agent id {agent_id!r}; known agents: {known}")
