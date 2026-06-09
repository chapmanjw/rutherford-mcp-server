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

from pydantic import ValidationError

from ..adapters.registry import AdapterRegistry
from ..domain.enums import DelegationMode, SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import Target

#: The per-target metadata keys read from a target dict, beyond ``cli`` and ``model``.
_TARGET_META_KEYS = ("role", "label", "weight", "parity", "stance")


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


def parse_strategy(value: str | Strategy) -> Strategy:
    """Coerce a consensus-strategy string to :class:`Strategy`, or raise ``INVALID_INPUT``."""
    if isinstance(value, Strategy):
        return value
    try:
        return Strategy(value)
    except ValueError:
        options = ", ".join(strategy.value for strategy in Strategy)
        raise RutherfordError(
            ErrorCode.INVALID_INPUT, f"unknown strategy {value!r}; choose one of: {options}"
        ) from None


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
    """Coerce a target into a :class:`Target`.

    Accepts a :class:`Target`; a ``cli`` / ``cli:model`` string; or a dict with ``cli`` (required),
    ``model``, and the optional per-seat metadata ``role`` / ``label`` / ``weight`` / ``parity`` /
    ``stance``. An invalid metadata value (e.g. an unknown stance) raises ``INVALID_INPUT``.
    """
    if isinstance(value, Target):
        return value
    if isinstance(value, dict):
        cli = value.get("cli")
        if not cli:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "each target needs a 'cli' field")
        fields: dict[str, Any] = {"cli": str(cli), "model": value.get("model")}
        fields.update({key: value[key] for key in _TARGET_META_KEYS if key in value})
        try:
            return Target(**fields)
        except ValidationError as exc:
            raise RutherfordError(ErrorCode.INVALID_INPUT, f"invalid target {value!r}: {exc}") from exc
    if isinstance(value, str):
        cli, _, model = value.partition(":")
        if not cli:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "target string must be 'cli' or 'cli:model'")
        return Target(cli=cli, model=model or None)
    raise RutherfordError(ErrorCode.INVALID_INPUT, f"cannot interpret target {value!r}")


def ensure_known_cli(registry: AdapterRegistry, cli_id: str) -> None:
    """Raise ``UNKNOWN_TARGET`` if ``cli_id`` is not a registered adapter.

    Called at the tool boundary so a typo'd CLI id is one clean error naming the known adapters,
    rather than (for consensus/debate) a buried failed *voice* the caller has to dig out.
    """
    if not registry.has(cli_id):
        known = ", ".join(registry.ids()) or "(none)"
        raise RutherfordError(ErrorCode.UNKNOWN_TARGET, f"unknown CLI id {cli_id!r}; known adapters: {known}")


def ensure_known_targets(registry: AdapterRegistry, targets: list[Target]) -> None:
    """Validate every target's ``cli`` against the registry (see :func:`ensure_known_cli`)."""
    for target in targets:
        ensure_known_cli(registry, target.cli)
