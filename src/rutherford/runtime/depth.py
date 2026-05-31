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

import os
from collections.abc import Mapping

from ..domain.error_codes import ErrorCode
from ..domain.errors import DepthLimitError, RutherfordError

#: The environment variable that carries the delegation depth across process boundaries.
ENV_DEPTH = "RUTHERFORD_DEPTH"


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
