# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared input parsing for the tool layer.

Tools accept simple client-friendly inputs (a CLI id and model string, a safety-mode string, a
list of target dicts). These helpers validate and coerce them into domain types, raising
:class:`~rutherford.domain.errors.RutherfordError` with ``INVALID_INPUT`` on bad input so the
FastMCP layer reports a clean error.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import DelegationMode, SafetyMode, Stance
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import Target


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


def parse_mode(value: str | DelegationMode) -> DelegationMode:
    """Coerce a sync/async mode string to :class:`DelegationMode`, or raise ``INVALID_INPUT``."""
    if isinstance(value, DelegationMode):
        return value
    try:
        return DelegationMode(value)
    except ValueError:
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown mode {value!r}; choose 'sync' or 'async'") from None


def parse_stances(values: list[str] | None) -> list[Stance] | None:
    """Coerce a list of stance strings to :class:`Stance` values, or raise ``INVALID_INPUT``."""
    if values is None:
        return None
    stances: list[Stance] = []
    for value in values:
        if isinstance(value, Stance):
            stances.append(value)
            continue
        try:
            stances.append(Stance(value))
        except ValueError:
            options = ", ".join(stance.value for stance in Stance)
            raise RutherfordError(
                ErrorCode.INVALID_INPUT, f"unknown stance {value!r}; choose one of: {options}"
            ) from None
    return stances


def as_target(value: Target | dict[str, Any] | str) -> Target:
    """Coerce a target given as a :class:`Target`, a ``{cli, model}`` dict, or a ``cli`` /
    ``cli:model`` string into a :class:`Target`."""
    if isinstance(value, Target):
        return value
    if isinstance(value, dict):
        cli = value.get("cli")
        if not cli:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "each target needs a 'cli' field")
        return Target(cli=str(cli), model=value.get("model"))
    if isinstance(value, str):
        cli, _, model = value.partition(":")
        if not cli:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "target string must be 'cli' or 'cli:model'")
        return Target(cli=cli, model=model or None)
    raise RutherfordError(ErrorCode.INVALID_INPUT, f"cannot interpret target {value!r}")
