# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Process-tree teardown for ACP agent subprocesses.

The ACP SDK's stdio transport terminates only the process it launched directly -- the npm adapter / node
entry point. An adapter that fronts a heavier CLI (``codex-acp`` -> the Codex CLI, ``claude-agent-acp`` ->
Claude Code) spawns that CLI as a child, and when the adapter exits the child is reparented and orphaned: it
keeps running, holds the working directory (so Windows then refuses to delete a temp probe dir, WinError 32),
and accumulates across repeated ``doctor`` probes. So Rutherford captures the agent's pid and, on session
close, reaps the descendant tree the transport leaves behind.

The snapshot must be taken *before* the parent is terminated: once a process dies its children reparent and
drop out of the walk from that pid (notably on Windows), so :func:`snapshot_descendants` runs first and
:func:`reap` runs after the connection is closed. Both are best-effort and never raise -- teardown must not
turn a good result into a failure.
"""

from __future__ import annotations

import contextlib

import psutil


def snapshot_descendants(pid: int) -> list[psutil.Process]:
    """The live descendant processes of ``pid`` (recursive), captured while the parent is still alive."""
    try:
        return psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return []


def count_descendants(pid: int) -> int:
    """Count ``pid`` itself plus its live descendants (N1, item 3), a FLOOR for the observed agent count.

    The same recursive walk :func:`snapshot_descendants` does, but reduced to a count and sampled while a
    turn is live -- the agent process plus every sub-process it spawned (the underlying CLI a wrapper fronts,
    and that CLI's own children). A FLOOR, not a ceiling: psutil sees only LOCAL processes, so an agent's
    remote/cloud sub-agents are invisible. ``0`` when the pid is already gone or psutil cannot read it, so a
    sample that loses the race never lowers a peak below a real one. Best-effort; never raises.
    """
    try:
        return 1 + len(psutil.Process(pid).children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return 0


def reap(procs: list[psutil.Process], *, grace_s: float = 2.0) -> None:
    """Terminate ``procs``, wait briefly, then kill any survivor. Best-effort; never raises."""
    if not procs:
        return
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=grace_s)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
