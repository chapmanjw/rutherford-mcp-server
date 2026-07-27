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
drop out of the walk from that pid (notably on Windows). Rutherford therefore snapshots first, hard-kills the
direct adapter so it cannot spawn replacements, reaps the captured descendants, and only then permits
EOF-sensitive transport cleanup. The synchronous primitives are best-effort and never raise -- teardown must
not turn a good result into a failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

import psutil

from ..runtime.logging import log_event


def snapshot_descendants(pid: int) -> list[psutil.Process]:
    """The live descendant processes of ``pid`` (recursive), captured while the parent is still alive."""
    try:
        return psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return []


async def snapshot_descendants_eagerly(pid: int) -> list[psutil.Process]:
    """Start a dedicated snapshot thread immediately and await its result without using the shared executor.

    The default executor can be saturated by file and terminal callbacks. Queueing this pre-parent-death snapshot
    there permits the adapter to exit before enumeration starts, after which reparented children are no longer
    discoverable from ``pid``. A short-lived daemon thread gives teardown its own execution slot; caller
    cancellation does not cancel the underlying snapshot.
    """
    result: Future[list[psutil.Process]] = Future()

    def _capture() -> None:
        try:
            result.set_result(snapshot_descendants(pid))
        except BaseException as exc:  # pragma: no cover - snapshot_descendants is best-effort and does not raise
            result.set_exception(exc)

    threading.Thread(
        target=_capture,
        name="rutherford-process-snapshot",
        daemon=True,
    ).start()
    return await asyncio.shield(asyncio.wrap_future(result))


#: The ONE owner of teardown work that outran its caller's deadline, for every path. Holding a strong
#: reference keeps the loop's weak task reference backed until the work finishes; entries remove themselves
#: on completion, so this is sized by concurrent in-flight cleanup TASKS, not by how many have ever run.
#: Tasks, not teardowns: an aggregate session close and whichever stage it is currently running are both
#: live work and both sit here, so one teardown in flight accounts for more than one entry.
#:
#: Deliberately uncapped. A cap can only be enforced by refusing to hold a reference, and an unreferenced
#: task is one the loop may collect mid-flight -- which is the drop these deadlines exist to prevent, moved
#: rather than removed. Growth here means teardown work is genuinely wedged, so the honest response is to
#: make that visible (:data:`_BACKLOG_WARN_EVERY`) rather than to discard the evidence.
_PENDING_CLEANUPS: set[asyncio.Task[Any]] = set()

#: Report the backlog once per this many outstanding cleanups, on the way UP only.
_BACKLOG_WARN_EVERY = 16

#: The outstanding count as of the last backlog report. Deliberately NOT a high-water mark: it rises when a
#: report is emitted and falls again as cleanups drain, which is what keeps the report once-per-step rather
#: than once-per-teardown. A plain "every Nth entry" test re-fires every time a backlog sitting on a step
#: boundary loses and regains a single task, and a mark that only ever rose would go silent for the life of
#: the process after one spike. The cost of letting it fall is that reports land at whatever level was
#: reached rather than at tidy multiples, so each one carries the threshold that triggered it.
_backlog_reported_at = 0

_T = TypeVar("_T")


def consume_task_result(task: asyncio.Task[Any]) -> None:
    """Swallow a detached task's terminal result so abandoned cleanup never warns about an unretrieved one."""
    with contextlib.suppress(BaseException):
        task.result()


def register_pending_cleanup(task: asyncio.Task[Any]) -> None:
    """Give ``task`` a durable owner until it completes, whatever its caller decides to do about waiting.

    Every teardown stage registers here -- the ones whose deadline cancels the work and the ones whose
    deadline does not. Both need the loop's weak task reference backed for as long as the task lives; only
    what happens at the deadline differs. Two retention schemes for one requirement is how the terminal and
    session paths drifted apart in the first place.
    """
    global _backlog_reported_at
    _PENDING_CLEANUPS.add(task)
    task.add_done_callback(_release_pending_cleanup)
    task.add_done_callback(consume_task_result)
    outstanding = len(_PENDING_CLEANUPS)
    # * The set grows one entry per call, so this fires the moment `outstanding` reaches the next level
    # exactly -- never past it. `outstanding` is therefore the whole story, and a separate "threshold"
    # field would only ever repeat it. Levels are NOT multiples of the step: the recorded level tracks the
    # set down, so a backlog that drains to 5 and climbs again next reports at 21, not 32.
    if outstanding >= _backlog_reported_at + _BACKLOG_WARN_EVERY:
        _backlog_reported_at = outstanding
        log_event(
            "acp_teardown_cleanup_backlog",
            level=logging.WARNING,
            outstanding=outstanding,
            task_name=task.get_name(),
        )


def _release_pending_cleanup(task: asyncio.Task[Any]) -> None:
    """Free a finished task's slot and let the reported backlog level fall with it."""
    global _backlog_reported_at
    _PENDING_CLEANUPS.discard(task)
    outstanding = len(_PENDING_CLEANUPS)
    if outstanding < _backlog_reported_at:
        _backlog_reported_at = outstanding


async def await_without_cancelling(
    coro: Coroutine[Any, Any, _T], *, timeout_s: float, task_name: str, default: _T
) -> _T:
    """Wait up to ``timeout_s`` for ``coro``, then abandon the WAIT -- never the work. ``default`` on expiry.

    ``asyncio.wait_for`` cancels its inner task on timeout, and for teardown that is precisely backwards.
    A coroutine like ``to_thread(reap, ...)`` that is still QUEUED on a saturated executor has not started,
    so the cancellation SUCCEEDS and the work never runs at all -- silently dropping a process tree that
    was already captured. Saturation is exactly the condition these deadlines exist for, so cancelling
    there converts "slow" into "never".

    The deadline is here to stop teardown BLOCKING its caller, not to stop it happening. So the task is
    retained and left to finish in the background, and only the waiting is bounded.
    """
    task = asyncio.create_task(coro, name=task_name)
    register_pending_cleanup(task)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if task not in done:
        return default
    try:
        return task.result()
    except (Exception, asyncio.CancelledError):
        return default


async def snapshot_within_deadline(pid: int, *, timeout_s: float, source: str) -> list[psutil.Process]:
    """Enumerate ``pid``'s descendants under a deadline, reaping a late result instead of discarding it.

    Shared by every pre-kill snapshot on purpose. Each caller runs this BEFORE hard-killing the process it
    is about to reap, which makes two properties load-bearing, and having two copies is how they drifted
    apart once already:

    * It must be bounded. An unbounded await never returns, so the caller never reaches its kill and its
      ``finally`` never runs either -- stranding the very process the teardown exists to collect.
    * A late result must still be reaped. The deadline exists to guarantee the kill, not to assert the
      tree is empty; a snapshot that lands just after it captured a real tree, and nothing sweeps that
      tree later because the next run enumerates from a different pid.

    ``source`` labels the log events. Retention is this function's own business, not a caller-supplied
    hook: an argument only one of two call sites passed is a difference between the paths waiting to become
    a divergence, and the shared owner already keeps a late snapshot alive until it can be reaped.
    """
    task = asyncio.create_task(snapshot_descendants_eagerly(pid), name=f"rutherford-snapshot-{source}")
    register_pending_cleanup(task)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if task in done:
        try:
            return task.result()
        except (Exception, asyncio.CancelledError):
            log_event("acp_teardown_snapshot_failed", level=logging.WARNING, source=source, pid=pid)
            return []
    # * Not an empty tree -- an unknown one. Say so, then let the late result reap itself.
    log_event("acp_teardown_snapshot_timeout", level=logging.WARNING, source=source, pid=pid, timeout_s=timeout_s)
    task.add_done_callback(functools.partial(_reap_late_snapshot, source=source))
    return []


def _reap_late_snapshot(task: asyncio.Task[list[psutil.Process]], *, source: str) -> None:
    """Reap a descendant list that arrived after its deadline, on a thread rather than the event loop.

    Runs as a done callback, which can fire while the loop is shutting down, so it must not depend on
    loop state. A daemon thread does the blocking reap; nothing awaits it, and it cannot outlive exit.
    """
    if task.cancelled() or task.exception() is not None:
        return
    late = task.result()
    if not late:
        return
    # * Dispatch FIRST, then report what actually happened. Logging "reaped" before starting the thread
    # makes the log lie whenever the start fails -- and Thread.start() raises RuntimeError for resource
    # exhaustion as well as interpreter finalization, so that is not only a shutdown-time concern. A
    # dropped tree that the log calls collected is worse than one it calls dropped.
    try:
        threading.Thread(target=reap, args=(late,), name=f"rutherford-late-reap-{source}", daemon=True).start()
    except RuntimeError as exc:
        log_event(
            "acp_teardown_late_snapshot_dropped",
            level=logging.WARNING,
            source=source,
            descendants=len(late),
            reason=str(exc),
        )
        return
    log_event("acp_teardown_late_snapshot_scheduled", level=logging.WARNING, source=source, descendants=len(late))


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
