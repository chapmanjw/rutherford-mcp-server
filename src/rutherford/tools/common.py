# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared input parsing for the tool layer (ACP-native).

Tools accept simple client-friendly inputs (an agent id, a safety-mode string). These helpers validate and
coerce them into domain types, raising :class:`~rutherford.domain.errors.RutherfordError` with
``INVALID_INPUT`` / ``UNKNOWN_TARGET`` on bad input so the FastMCP layer reports a clean error.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..acp.descriptors import DescriptorRegistry
from ..domain.enums import DelegationMode, Effort, SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import OnBudget, Target
from ..services.roles import RoleStore

#: The valid ``on_budget`` dispositions, for validating the tool-layer string against the Literal type.
_ON_BUDGET: tuple[str, ...] = ("harvest", "continue", "resume")

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


def resolve_safety_mode(value: str | SafetyMode | None, default: SafetyMode) -> SafetyMode:
    """The effective safety mode for a call: the explicit value, else the configured default.

    ``None`` means the caller omitted the field -- the one case the configured ``default_safety_mode``
    fills -- so an explicit choice always wins over config.
    """
    if value is None:
        return default
    return parse_safety_mode(value)


def resolve_run_mode(value: str | DelegationMode) -> bool:
    """Coerce a run-mode string to a boolean ``run_async``, or raise ``INVALID_INPUT``.

    ``"sync"`` (the default) runs the work on the request path; ``"async"`` submits it as a background
    job. Returns ``True`` for async so the caller can branch on one bool. A typoed mode fails here, on
    the request path, rather than being silently treated as sync.
    """
    if isinstance(value, DelegationMode):
        return value is DelegationMode.ASYNC
    try:
        return DelegationMode(value) is DelegationMode.ASYNC
    except ValueError:
        options = ", ".join(item.value for item in DelegationMode)
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown mode {value!r}; choose one of: {options}") from None


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


def parse_effort(value: str | Effort | None) -> Effort | None:
    """Coerce a reasoning-effort string to :class:`Effort`, or raise ``INVALID_INPUT``; ``None`` passes through.

    ``None`` means the caller omitted ``effort`` -- the one case the configured ``default_effort`` (or a
    per-agent ``effort``) fills downstream -- so it is preserved here rather than coerced to a tier.
    """
    if value is None or isinstance(value, Effort):
        return value
    try:
        return Effort(value)
    except ValueError:
        options = ", ".join(effort.value for effort in Effort)
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown effort {value!r}; choose one of: {options}") from None


def parse_on_budget(value: str | None) -> OnBudget | None:
    """Coerce an ``on_budget`` string to the :data:`OnBudget` literal, or raise ``INVALID_INPUT``.

    ``None`` means the caller omitted it -- the configured ``default_on_budget`` (``harvest`` out of the box)
    applies downstream -- so it is preserved.
    """
    if value is None:
        return None
    if value in _ON_BUDGET:
        return value  # type: ignore[return-value]  # validated against the Literal's members
    options = ", ".join(_ON_BUDGET)
    raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown on_budget {value!r}; choose one of: {options}") from None


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


def ensure_known_agent(descriptors: DescriptorRegistry, agent_id: str) -> None:
    """Raise ``UNKNOWN_TARGET`` if ``agent_id`` is not a registered ACP agent."""
    if not descriptors.has(agent_id):
        known = ", ".join(descriptors.ids()) or "(none)"
        raise RutherfordError(ErrorCode.UNKNOWN_TARGET, f"unknown agent id {agent_id!r}; known agents: {known}")


def as_target(value: Target | dict[str, Any] | str) -> Target:
    """Coerce a target into a :class:`Target`.

    Accepts a :class:`Target`; a ``cli`` / ``cli:model`` string; or a dict with ``cli`` (required),
    ``model``, and the optional per-seat metadata ``role`` / ``label`` / ``weight`` / ``parity`` /
    ``stance``. An invalid metadata value (e.g. an unknown stance, a negative weight) raises
    ``INVALID_INPUT``.
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
            raise RutherfordError(ErrorCode.INVALID_INPUT, "a target string must be 'cli' or 'cli:model'")
        return Target(cli=cli, model=model or None)
    raise RutherfordError(ErrorCode.INVALID_INPUT, f"cannot interpret target {value!r}")


def ensure_known_targets(descriptors: DescriptorRegistry, targets: list[Target]) -> None:
    """Validate every target's ``cli`` against the registry (see :func:`ensure_known_agent`)."""
    for target in targets:
        ensure_known_agent(descriptors, target.cli)


def apply_role(roles: RoleStore, role: str | None, prompt: str) -> str:
    """Prepend role ``role``'s persona to ``prompt`` when one is named, else return ``prompt`` unchanged.

    The single role seam for ``delegate`` / ``consensus`` / ``debate``: a named role is validated against
    the store (a bad id raises ``UNKNOWN_ROLE`` on the request path, listing the known roles) and its
    prompt is prepended; an omitted role is a no-op. The role is folded into the prompt here, at the tool
    layer, so the services keep handing one composed prompt to the agent.
    """
    if role is None:
        return prompt
    return roles.apply(role, prompt)
