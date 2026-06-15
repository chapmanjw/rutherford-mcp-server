# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The delegation depth guard and the cross-process lineage signal (N1, item 3).

An ACP agent Rutherford spawns can itself be a Rutherford host (a CLI that fronts this very MCP server),
so a Rutherford-driving-Rutherford chain could recurse without bound. Rutherford tracks a delegation
depth, propagates it to every spawned ACP agent through ``RUTHERFORD_DEPTH``, and refuses to spawn past a
configured maximum so the chain stops instead of recursing forever. Alongside it, a count-first lineage
signal (``RUTHERFORD_LINEAGE`` plus the parent run id in ``RUTHERFORD_PARENT_RUN``) lets a nested host see
where it sits in the agent tree, so an aggregate-agent cap can reason about total fan-out across layers.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError

#: The environment variable that carries the delegation depth across process boundaries.
ENV_DEPTH = "RUTHERFORD_DEPTH"

#: The id of the parent run, propagated to a spawned child so a nested Rutherford (or an external
#: orchestrator) can correlate the child's runs back to the panel that launched it. Count-first: the count
#: below is the primary in-process signal, so this id is WRITTEN for correlation but has no in-process reader
#: (nothing in Rutherford reads its own parent id back). Unset at the top level.
ENV_PARENT_RUN = "RUTHERFORD_PARENT_RUN"

#: How many Rutherford layers deep this run is, propagated to children incremented by one. A lineage of
#: nested orchestrators (a CLI that is itself a Rutherford host) reads count-first, so the aggregate-agent
#: cap can reason about total fan-out across layers, not just this layer's width.
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


def child_depth_env(depth: int) -> dict[str, str]:
    """Return the environment overlay that records a child's delegation depth (``depth + 1``)."""
    return {ENV_DEPTH: str(depth + 1)}


def child_lineage_env(parent_run_id: str | None = None, current_count: int = 0) -> dict[str, str]:
    """Return the environment overlay recording a child's lineage (count incremented, parent id carried).

    Count-first: always sets :data:`ENV_LINEAGE` to ``current_count + 1`` so each nested Rutherford layer is
    countable; includes :data:`ENV_PARENT_RUN` only when a parent run id is known.
    """
    env: dict[str, str] = {ENV_LINEAGE: str(current_count + 1)}
    if parent_run_id is not None:
        env[ENV_PARENT_RUN] = parent_run_id
    return env


def child_env(depth: int, *, parent_run_id: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The full lineage/depth overlay handed to a spawned ACP agent: depth incremented + lineage count.

    Combines :func:`child_depth_env` (the recursion guard the child reads back through :func:`current_depth`)
    with :func:`child_lineage_env` (the count-first lineage signal), reading the current lineage count from
    ``env`` (the process environment by default). The one place a service layers the cross-process N1 env
    onto a session, mirroring how the per-call effort override is layered.
    """
    return {
        **child_depth_env(depth),
        **child_lineage_env(parent_run_id=parent_run_id, current_count=current_lineage_count(env)),
    }


def ensure_within_depth(depth: int, max_depth: int) -> None:
    """Raise :class:`RutherfordError` (``MAX_DEPTH_EXCEEDED``) if a delegation at ``depth`` would exceed the cap.

    A delegation may run at depths ``0 .. max_depth - 1``; at ``max_depth`` it is refused, so a
    self-referential Rutherford chain stops instead of recursing forever.
    """
    if depth >= max_depth:
        raise RutherfordError(
            ErrorCode.MAX_DEPTH_EXCEEDED,
            f"delegation depth {depth} reaches the maximum of {max_depth}; refusing to spawn deeper",
            details={"depth": depth, "max_depth": max_depth},
        )


def ensure_within_aggregate_cap(declared_width: int, aggregate_cap: int | None, *, enforce: bool = False) -> bool:
    """Check a panel's declared width against the advisory aggregate-agent cap (N1, item 3).

    The cap is checked at DECLARATION time -- the one moment Rutherford can act on it cheaply and soundly
    (the realized count, once agents spawn their own sub-agents, is unknown and only observed after the
    fact). Returns ``True`` when the width is OVER the cap (the caller flags ``Topology.over_cap`` and logs a
    warning), ``False`` when within it. With ``enforce=True`` an over-cap width is refused up front with
    ``AGENT_CAP_EXCEEDED`` instead of returning ``True``. ``None`` disables the cap (the default config), so
    a width is always within it.
    """
    if aggregate_cap is None or declared_width <= aggregate_cap:
        return False
    if enforce:
        raise RutherfordError(
            ErrorCode.AGENT_CAP_EXCEEDED,
            f"declared panel width {declared_width} exceeds the aggregate-agent cap {aggregate_cap}; "
            "refusing up front (enforce_agent_cap is set)",
            details={"declared": declared_width, "cap": aggregate_cap},
        )
    return True
