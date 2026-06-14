# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared input parsing for the tool layer (ACP-native).

Tools accept simple client-friendly inputs (an agent id, a safety-mode string). These helpers validate and
coerce them into domain types, raising :class:`~rutherford.domain.errors.RutherfordError` with
``INVALID_INPUT`` / ``UNKNOWN_TARGET`` on bad input so the FastMCP layer reports a clean error.
"""

from __future__ import annotations

from typing import Any

from ..acp.descriptors import DescriptorRegistry
from ..domain.enums import DelegationMode, SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import Target
from ..services.roles import RoleStore


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


def ensure_known_agent(descriptors: DescriptorRegistry, agent_id: str) -> None:
    """Raise ``UNKNOWN_TARGET`` if ``agent_id`` is not a registered ACP agent."""
    if not descriptors.has(agent_id):
        known = ", ".join(descriptors.ids()) or "(none)"
        raise RutherfordError(ErrorCode.UNKNOWN_TARGET, f"unknown agent id {agent_id!r}; known agents: {known}")


def as_target(value: Target | dict[str, Any] | str) -> Target:
    """Coerce a target into a :class:`Target`: a ``Target``, a ``cli`` / ``cli:model`` string, or a dict
    with ``cli`` (required) and optional ``model``. Raises ``INVALID_INPUT`` on a malformed target."""
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
