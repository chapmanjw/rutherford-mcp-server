# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Per-adapter cooldown (F7): bench a flapping CLI so it stops dragging on every panel.

A CLI that is down, throttled, or mis-launching will fail repeatedly. Without a memory, an auto-panel
re-includes it every call and a fallback chain keeps reaching for it -- each time paying the failure's
latency. :class:`CooldownTracker` keeps a small in-memory record of recent *unhealthy* failures per
adapter (see :func:`~rutherford.runtime.failures.indicates_unhealthy`) and, once an adapter crosses a
threshold within a window, benches it for a fixed duration. A benched adapter is left out of an
auto-expanded consensus panel and skipped as a fallback candidate -- but an *explicit* delegation to it
still runs, because the caller chose it on purpose.

Cooldown keys on the *adapter id*, not the provider: three CLIs all fronting one provider each trip
independently, so a single provider-wide rate-limit storm benches whichever adapter hit it enough,
not the provider. That is a deliberate scope -- cross-provider diversity is the recovery path -- not
provider-level backoff.

The state is process-global (one tracker on the shared delegation service) and resets on restart. The
clock is injected so the windows are unit-testable without sleeping. Setting the threshold to ``0``
disables cooldown entirely.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class CooldownTracker:
    """Tracks recent failures per adapter and benches one that flaps past a threshold."""

    def __init__(
        self,
        *,
        threshold: int,
        window_s: float,
        duration_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: Unhealthy failures within ``window_s`` that trip a bench; ``<= 0`` disables cooldown.
        self._threshold = threshold
        self._window_s = window_s
        self._duration_s = duration_s
        self._clock = clock
        #: adapter id -> timestamps of recent unhealthy failures (pruned to the window).
        self._failures: dict[str, list[float]] = {}
        #: adapter id -> monotonic time the bench lifts.
        self._benched_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        """Whether cooldown is active (a positive threshold)."""
        return self._threshold > 0

    def record_failure(self, adapter_id: str) -> None:
        """Record one unhealthy failure for ``adapter_id``; bench it if it crosses the threshold.

        Old failures outside the window are dropped first, so the threshold is "this many failures
        *within* the window". Crossing it benches the adapter for ``duration_s`` and clears the streak,
        so a benched adapter starts fresh when its bench lifts rather than re-tripping immediately.
        """
        if not self.enabled:
            return
        now = self._clock()
        recent = [t for t in self._failures.get(adapter_id, []) if now - t < self._window_s]
        recent.append(now)
        if len(recent) >= self._threshold:
            self._benched_until[adapter_id] = now + self._duration_s
            self._failures.pop(adapter_id, None)
        else:
            self._failures[adapter_id] = recent

    def record_success(self, adapter_id: str) -> None:
        """Clear ``adapter_id``'s failure streak on a success (the bench, being time-based, stays)."""
        if not self.enabled:
            return
        self._failures.pop(adapter_id, None)

    def is_benched(self, adapter_id: str) -> bool:
        """Whether ``adapter_id`` is currently benched (its bench has not yet lifted)."""
        if not self.enabled:
            return False
        until = self._benched_until.get(adapter_id)
        return until is not None and self._clock() < until

    def remaining_s(self, adapter_id: str) -> float:
        """Seconds until ``adapter_id``'s bench lifts, or ``0.0`` if it is not benched."""
        until = self._benched_until.get(adapter_id)
        if until is None:
            return 0.0
        return max(0.0, until - self._clock())
