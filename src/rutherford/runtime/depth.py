# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The delegation depth guard and per-request target cap.

A self-invocation is a fresh, isolated subprocess, so a CLI delegating to its own adapter could
recurse without bound. Rutherford tracks a delegation depth, propagates it to spawned children
through the ``RUTHERFORD_DEPTH`` environment variable, refuses to spawn beyond a configured
maximum depth, and caps the number of targets per call. Together these keep a CLI-calls-itself
chain safe and bounded.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from ..domain.error_codes import ErrorCode
from ..domain.errors import DepthLimitError, RutherfordError

_log = logging.getLogger("rutherford.runtime.depth")

#: The environment variable that carries the delegation depth across process boundaries.
ENV_DEPTH = "RUTHERFORD_DEPTH"

#: The id of the parent run, propagated to a spawned child so a nested Rutherford or an external orchestrator
#: can correlate the child's runs back to the panel that launched it (N1, item 3). Count-first: the count
#: below is the primary in-process signal, so this id is WRITTEN for correlation but has no in-process reader
#: (nothing in Rutherford reads its own parent id back). ``None`` (unset) at the top level.
ENV_PARENT_RUN = "RUTHERFORD_PARENT_RUN"
#: How many Rutherford layers deep this run is, propagated to children incremented by one (N1, item 3). A
#: lineage of nested orchestrators (a CLI that is itself a Rutherford host) reads count-first, so the
#: aggregate-agent cap can reason about total fan-out across layers, not just this layer's width.
ENV_LINEAGE = "RUTHERFORD_LINEAGE"


def current_depth(env: Mapping[str, str] | None = None) -> int:
    """Read the current delegation depth from the environment (0 when unset or invalid)."""
    environ = os.environ if env is None else env
    raw = environ.get(ENV_DEPTH)
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def child_depth_env(depth: int) -> dict[str, str]:
    """Return the environment overlay that records the child's depth (``depth + 1``)."""
    return {ENV_DEPTH: str(depth + 1)}


def ensure_within_depth(depth: int, max_depth: int) -> None:
    """Raise :class:`DepthLimitError` if a delegation at ``depth`` would exceed ``max_depth``.

    A delegation may run at depths ``0 .. max_depth - 1``; at ``max_depth`` it is refused, so a
    self-referential chain stops instead of recursing forever.
    """
    if depth >= max_depth:
        raise DepthLimitError(
            f"delegation depth {depth} reaches the maximum of {max_depth}; refusing to spawn deeper",
            details={"depth": depth, "max_depth": max_depth},
        )


def ensure_within_target_cap(count: int, max_targets: int) -> None:
    """Raise if a call fans out to more than ``max_targets`` targets."""
    if count > max_targets:
        raise RutherfordError(
            ErrorCode.TOO_MANY_TARGETS,
            f"requested {count} targets, but the per-call cap is {max_targets}",
            details={"requested": count, "max_targets": max_targets},
        )


def current_lineage_count(env: Mapping[str, str] | None = None) -> int:
    """Read the Rutherford lineage depth from the environment (0 when unset or invalid)."""
    environ = os.environ if env is None else env
    raw = environ.get(ENV_LINEAGE)
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def child_lineage_env(parent_run_id: str | None = None, current_count: int = 0) -> dict[str, str]:
    """Return the environment overlay recording a child's lineage (count incremented, parent id carried).

    Count-first (N1, item 3): always sets :data:`ENV_LINEAGE` to ``current_count + 1`` so each nested
    Rutherford layer is countable; includes :data:`ENV_PARENT_RUN` only when a parent run id is known.
    """
    env: dict[str, str] = {ENV_LINEAGE: str(current_count + 1)}
    if parent_run_id is not None:
        env[ENV_PARENT_RUN] = parent_run_id
    return env


def ensure_within_aggregate_cap(declared_width: int, aggregate_cap: int | None, *, enforce: bool = False) -> None:
    """Check a panel's declared width against the advisory aggregate-agent cap (N1, item 3).

    The cap is checked at DECLARATION time -- the one moment Rutherford can act on it cheaply and soundly
    (the realized count, once CLIs spawn their own agents, is unknown and only observed after the fact). By
    default (``enforce=False``) an over-cap width is a logged warning, not a refusal: the goal is to make
    runaway fan-out VISIBLE (watch the ``activity`` view, cancel if needed), not to block it. With
    ``enforce=True`` an over-cap width is refused up front with ``AGENT_CAP_EXCEEDED``. ``None`` disables the
    cap entirely (the default config). A width within the cap is always a no-op.
    """
    if aggregate_cap is None or declared_width <= aggregate_cap:
        return
    message = (
        f"declared panel width {declared_width} exceeds the aggregate-agent cap {aggregate_cap}; "
        "the panel may spawn many agents -- watch the activity view and cancel if needed"
    )
    if enforce:
        raise RutherfordError(
            ErrorCode.AGENT_CAP_EXCEEDED,
            message,
            details={"declared": declared_width, "cap": aggregate_cap},
        )
    _log.warning(message)
