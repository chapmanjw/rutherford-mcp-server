# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""A caching, timeout-capping decorator over :class:`~rutherford.runtime.probe.CommandProbe`.

Adapter metadata probes (``which`` the binary, read ``--version``, ask an auth-status command, list
models) are pure and read-only, yet ``capabilities`` / ``doctor`` / ``consensus`` auto-expansion call
them repeatedly within seconds and re-fork the same subprocesses each time. :class:`CachingProbe`
wraps the real probe with a short-TTL cache keyed on the resolved name / argv, and caps every probe
at a hard ceiling so a CLI whose ``--version`` hangs cannot stall the whole capability snapshot. It
is injected as the ``CommandProbe`` at registry-build time, so every adapter benefits transparently
with no adapter change; a hung probe's ``timed_out`` result is itself cached, so one bad CLI is not
retried within the TTL. Setting ``ttl_s=0`` disables caching (every call re-probes).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..domain.models import ProcessResult
from .probe import CommandProbe


class CachingProbe:
    """A :class:`CommandProbe` decorator: short-TTL result cache plus a per-probe timeout ceiling."""

    def __init__(
        self,
        inner: CommandProbe,
        *,
        ttl_s: float = 10.0,
        ceiling_s: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._ttl_s = ttl_s
        self._ceiling_s = ceiling_s
        self._clock = clock
        self._which_cache: dict[str, tuple[float, str | None]] = {}
        self._run_cache: dict[
            tuple[tuple[str, ...], frozenset[tuple[str, str]], float], tuple[float, ProcessResult]
        ] = {}

    def which(self, name: str) -> str | None:
        """Resolve ``name`` on PATH, caching the result for the TTL."""
        if self._ttl_s <= 0:
            return self._inner.which(name)
        now = self._clock()
        cached = self._which_cache.get(name)
        if cached is not None and now - cached[0] <= self._ttl_s:
            return cached[1]
        value = self._inner.which(name)
        self._which_cache[name] = (now, value)
        return value

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Run ``argv``, capped at the ceiling and cached for the TTL by ``(argv, env, timeout)``.

        The effective timeout is part of the key so a ``timed_out=True`` produced under a short
        ceiling is never served to a later same-command call that asked for (and would have
        succeeded under) a longer one.
        """
        effective_timeout = min(timeout_s, self._ceiling_s)
        if self._ttl_s <= 0:
            return self._inner.run(argv, timeout_s=effective_timeout, env=env)
        key = (tuple(argv), frozenset((env or {}).items()), effective_timeout)
        now = self._clock()
        cached = self._run_cache.get(key)
        if cached is not None and now - cached[0] <= self._ttl_s:
            return cached[1]
        result = self._inner.run(argv, timeout_s=effective_timeout, env=env)
        self._run_cache[key] = (now, result)
        return result

    def invalidate(self) -> None:
        """Drop all cached results, so the next probe re-runs the underlying command.

        ``doctor``'s live verification calls this before its diagnostic probes, so a freshly logged-in
        CLI is re-read rather than served from a stale cache entry.
        """
        self._which_cache.clear()
        self._run_cache.clear()
