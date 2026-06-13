# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared input parsing for the tool layer.

Tools accept simple client-friendly inputs (a CLI id and model string, a safety-mode string, a
list of target dicts). These helpers validate and coerce them into domain types, raising
:class:`~rutherford.domain.errors.RutherfordError` with ``INVALID_INPUT`` on bad input so the
FastMCP layer reports a clean error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..adapters.registry import AdapterRegistry
from ..domain.enums import DelegationMode, SafetyMode, Stance, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import Target

if TYPE_CHECKING:
    from ..context import AppContext
    from ..domain.models import Job

#: The per-target metadata keys read from a target dict, beyond ``cli`` and ``model``.
_TARGET_META_KEYS = ("role", "label", "weight", "parity", "stance")


def async_job_envelope(
    app: AppContext, job: Job, *, persist: bool | None, complex_run: bool, external_tracking: bool
) -> dict[str, Any]:
    """Build the ``mode=async`` submit envelope, carrying the same F2 persistence notice the sync path adds.

    A non-trivial run started as a background job is exactly the case decision 1-J targets, but the async
    path returns at submit time before any result exists -- so the notice is computed here from whether the
    run *will* persist (``persist``, else the configured ``default_persistence``). ``None`` notice is
    omitted so the envelope stays minimal when there is nothing to say.
    """
    would_persist = app.config.wants_persist(persist)
    notice = app.persistence_notice(
        persisted=would_persist, complex_run=complex_run, external_tracking=external_tracking
    )
    payload: dict[str, Any] = {"job_id": job.id, "status": job.status, "kind": job.kind}
    if notice is not None:
        payload["notice"] = notice
    return payload


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


def parse_persistence(value: str) -> str:
    """Validate a ``default_persistence`` choice (``ephemeral`` | ``job``) for setup, or raise ``INVALID_INPUT``.

    Kept as a string (the config field is a ``Literal``, not an enum) but validated at the tool boundary so
    an invalid value is a clean error here, never written into config where it would fail the next load.
    """
    if value in ("ephemeral", "job"):
        return value
    raise RutherfordError(
        ErrorCode.INVALID_INPUT, f"unknown default_persistence {value!r}; choose one of: ephemeral, job"
    )


def parse_scope(value: str) -> str:
    """Validate a setup ``scope`` (``global`` | ``project``), or raise ``INVALID_INPUT``."""
    if value in ("global", "project"):
        return value
    raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown scope {value!r}; choose one of: global, project")


def resolve_safety_mode(value: str | SafetyMode | None, default: SafetyMode) -> SafetyMode:
    """The effective safety mode for a call: the explicit value, else the configured default.

    ``None`` means the caller omitted the field -- the one case the configured
    ``default_safety_mode`` is documented to fill. The ``None`` sentinel (not a ``"read_only"``
    string default on the tool signature) is what lets the tool layer tell "omitted" apart from
    "explicitly read_only", so an explicit choice always wins over config. An explicit value still
    validates through :func:`parse_safety_mode`.
    """
    if value is None:
        return default
    return parse_safety_mode(value)


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
