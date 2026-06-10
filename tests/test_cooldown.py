# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the F7 cooldown tracker, with an injected clock so windows need no real sleeping."""

from __future__ import annotations

from rutherford.runtime.cooldown import CooldownTracker


class _Clock:
    """A hand-cranked monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(
    clock: _Clock, *, threshold: int = 3, window_s: float = 120.0, duration_s: float = 60.0
) -> CooldownTracker:
    return CooldownTracker(threshold=threshold, window_s=window_s, duration_s=duration_s, clock=clock)


def test_benches_after_threshold_failures_in_window() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=3)
    tracker.record_failure("a")
    tracker.record_failure("a")
    assert not tracker.is_benched("a")  # 2 < 3
    tracker.record_failure("a")
    assert tracker.is_benched("a")  # 3rd trips it


def test_old_failures_outside_window_do_not_count() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=2, window_s=100.0)
    tracker.record_failure("a")
    clock.advance(101.0)  # the first failure ages out of the window
    tracker.record_failure("a")
    assert not tracker.is_benched("a")  # only one failure is within the window


def test_bench_lifts_after_duration() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=1, duration_s=60.0)
    tracker.record_failure("a")
    assert tracker.is_benched("a")
    assert 0 < tracker.remaining_s("a") <= 60.0
    clock.advance(60.0)
    assert not tracker.is_benched("a")  # the bench has lifted
    assert tracker.remaining_s("a") == 0.0


def test_success_clears_the_failure_streak() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=2)
    tracker.record_failure("a")
    tracker.record_success("a")  # streak reset
    tracker.record_failure("a")
    assert not tracker.is_benched("a")  # only one failure since the success


def test_success_does_not_lift_an_active_bench() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=1, duration_s=60.0)
    tracker.record_failure("a")
    tracker.record_success("a")
    assert tracker.is_benched("a")  # the bench is time-based; a success does not cut it short


def test_threshold_zero_disables_cooldown() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=0)
    assert not tracker.enabled
    for _ in range(10):
        tracker.record_failure("a")
    tracker.record_success("a")  # also a no-op when disabled
    assert not tracker.is_benched("a")


def test_remaining_is_zero_for_a_never_benched_adapter() -> None:
    tracker = _tracker(_Clock(), threshold=1)
    assert tracker.remaining_s("never-seen") == 0.0


def test_adapters_are_tracked_independently() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=1)
    tracker.record_failure("a")
    assert tracker.is_benched("a")
    assert not tracker.is_benched("b")


def test_benching_resets_the_streak_so_it_does_not_immediately_retrip() -> None:
    clock = _Clock()
    tracker = _tracker(clock, threshold=2, duration_s=10.0)
    tracker.record_failure("a")
    tracker.record_failure("a")  # benched, streak cleared
    assert tracker.is_benched("a")
    clock.advance(10.0)  # bench lifts
    tracker.record_failure("a")
    assert not tracker.is_benched("a")  # one failure post-bench, not two
