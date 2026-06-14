# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the per-agent cooldown tracker (F7): bench after N failures, un-bench after the duration."""

from __future__ import annotations

from rutherford.acp.cooldown import CooldownTracker


class _Clock:
    """A hand-advanced monotonic clock, so the windows are tested without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tracker(
    *, threshold: int = 3, window_s: float = 120.0, duration_s: float = 60.0
) -> tuple[CooldownTracker, _Clock]:
    clock = _Clock()
    return CooldownTracker(threshold=threshold, window_s=window_s, duration_s=duration_s, clock=clock), clock


def test_benches_after_threshold_failures_within_window() -> None:
    tracker, _clock = _tracker(threshold=3)
    tracker.record_failure("goose")
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is False  # two failures, below the threshold of three
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is True  # the third trips the bench


def test_unbenches_after_the_duration() -> None:
    tracker, clock = _tracker(threshold=2, duration_s=60.0)
    tracker.record_failure("goose")
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is True
    assert tracker.remaining_s("goose") == 60.0
    clock.now = 59.9
    assert tracker.is_benched("goose") is True
    clock.now = 60.0
    assert tracker.is_benched("goose") is False  # the bench lifts exactly at the duration
    assert tracker.remaining_s("goose") == 0.0


def test_old_failures_outside_the_window_do_not_count() -> None:
    tracker, clock = _tracker(threshold=2, window_s=100.0)
    tracker.record_failure("goose")
    clock.now = 150.0  # the first failure is now outside the 100s window
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is False  # the stale failure was pruned, so this is only the first


def test_record_success_resets_the_streak() -> None:
    tracker, _clock = _tracker(threshold=3)
    tracker.record_failure("goose")
    tracker.record_failure("goose")
    tracker.record_success("goose")  # a clean turn clears the streak
    tracker.record_failure("goose")
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is False  # only two failures since the reset
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is True


def test_keyed_per_agent_independently() -> None:
    tracker, _clock = _tracker(threshold=2)
    tracker.record_failure("goose")
    tracker.record_failure("goose")
    assert tracker.is_benched("goose") is True
    assert tracker.is_benched("opencode") is False  # a different agent's bench is independent


def test_zero_threshold_disables_cooldown() -> None:
    tracker, _clock = _tracker(threshold=0)
    assert tracker.enabled is False
    for _ in range(10):
        tracker.record_failure("goose")
    assert tracker.is_benched("goose") is False  # disabled: no agent is ever benched
    assert tracker.remaining_s("goose") == 0.0


def test_remaining_s_for_an_unbenched_agent_is_zero() -> None:
    tracker, _clock = _tracker()
    assert tracker.remaining_s("never-failed") == 0.0
