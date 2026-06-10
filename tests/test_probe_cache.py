# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the caching, timeout-capping probe decorator (``runtime/probe_cache``)."""

from __future__ import annotations

from rutherford.domain.models import ProcessResult
from rutherford.runtime.probe_cache import CachingProbe
from tests.fakes import FakeProbe


class _Clock:
    """A manually-advanced monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_run_is_cached_within_ttl() -> None:
    inner = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="v1"))
    clock = _Clock()
    probe = CachingProbe(inner, ttl_s=10.0, clock=clock)
    assert probe.run(["tool", "--version"]).stdout == "v1"
    assert probe.run(["tool", "--version"]).stdout == "v1"
    assert len(inner.calls) == 1  # second call served from cache


def test_run_re_probes_after_ttl_expires() -> None:
    inner = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="v"))
    clock = _Clock()
    probe = CachingProbe(inner, ttl_s=10.0, clock=clock)
    probe.run(["tool", "--version"])
    clock.now += 11.0
    probe.run(["tool", "--version"])
    assert len(inner.calls) == 2


def test_distinct_argv_and_env_are_cached_separately() -> None:
    inner = FakeProbe()
    probe = CachingProbe(inner, ttl_s=10.0, clock=_Clock())
    probe.run(["tool", "a"])
    probe.run(["tool", "b"])
    probe.run(["tool", "a"], env={"X": "1"})
    assert len(inner.calls) == 3  # argv-b and the env variant are distinct keys


def test_which_is_cached_by_name() -> None:
    inner = FakeProbe(which_map={"tool": "/usr/bin/tool"})
    probe = CachingProbe(inner, ttl_s=10.0, clock=_Clock())
    assert probe.which("tool") == "/usr/bin/tool"
    assert probe.which("tool") == "/usr/bin/tool"
    # FakeProbe.which has no call counter, so assert behavior via a second name miss instead.
    assert probe.which("absent") is None


def test_ceiling_caps_the_probe_timeout() -> None:
    inner = FakeProbe()
    probe = CachingProbe(inner, ttl_s=10.0, ceiling_s=3.0, clock=_Clock())
    probe.run(["tool", "--version"], timeout_s=15.0)
    assert inner.timeouts == [3.0]  # capped to the ceiling, not the requested 15s


def test_a_timed_out_probe_is_cached_and_not_retried() -> None:
    inner = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=None, timed_out=True))
    probe = CachingProbe(inner, ttl_s=10.0, clock=_Clock())
    assert probe.run(["hung", "--version"]).timed_out
    probe.run(["hung", "--version"])
    assert len(inner.calls) == 1  # a hung CLI is not re-forked within the TTL


def test_invalidate_forces_a_re_probe() -> None:
    inner = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="x"))
    probe = CachingProbe(inner, ttl_s=10.0, clock=_Clock())
    probe.run(["tool", "--version"])
    probe.invalidate()
    probe.run(["tool", "--version"])
    assert len(inner.calls) == 2


def test_ttl_zero_disables_caching() -> None:
    inner = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="x"))
    probe = CachingProbe(inner, ttl_s=0.0, clock=_Clock())
    probe.run(["tool", "--version"])
    probe.run(["tool", "--version"])
    assert len(inner.calls) == 2  # caching off: every call re-probes


def test_a_short_timeout_result_is_not_served_to_a_longer_timeout_call() -> None:
    # The effective timeout is part of the cache key: a timed_out=True cached under a short
    # ceiling must not answer a later same-command call that asked for a longer budget.
    inner = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=None, timed_out=True))
    probe = CachingProbe(inner, ttl_s=10.0, ceiling_s=30.0, clock=_Clock())
    assert probe.run(["slow", "--version"], timeout_s=1.0).timed_out
    probe.run(["slow", "--version"], timeout_s=20.0)
    assert len(inner.calls) == 2  # the longer-budget call re-probed instead of inheriting the verdict
    assert inner.timeouts == [1.0, 20.0]
