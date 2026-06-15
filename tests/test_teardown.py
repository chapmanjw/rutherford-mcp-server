# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for process-tree teardown: snapshot the descendants of a live process and reap the tree.

These spawn real (local, fast) Python subprocess trees -- no network, no agent -- to prove that a wrapper
adapter's orphaned CLI child would actually be killed on session close.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import psutil

from rutherford.acp.teardown import reap, snapshot_descendants

# A parent that spawns a long-sleeping child, prints the child's pid, then sleeps itself.
_PARENT_WITH_CHILD = (
    "import subprocess, sys, time; "
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "print(c.pid, flush=True); "
    "time.sleep(60)"
)


def test_snapshot_missing_pid_is_empty() -> None:
    # An implausibly high pid does not exist -> a clean empty snapshot, not an error.
    assert snapshot_descendants(2_000_000_000) == []


def test_reap_empty_is_a_noop() -> None:
    reap([])  # must not raise


async def test_reap_terminates_a_process() -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)")
    handle = psutil.Process(proc.pid)
    assert handle.is_running()
    await asyncio.to_thread(reap, [handle])
    await proc.wait()  # collect the (now dead) child so its pid is fully released on POSIX
    assert not psutil.pid_exists(proc.pid) or not handle.is_running()


async def test_snapshot_and_reap_kills_the_whole_tree() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _PARENT_WITH_CHILD, stdout=asyncio.subprocess.PIPE
    )
    assert proc.stdout is not None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
        child_pid = int(line.strip())
        descendants: list[psutil.Process] = []
        for _ in range(50):  # give psutil a beat to see the freshly spawned child
            descendants = await asyncio.to_thread(snapshot_descendants, proc.pid)
            if any(d.pid == child_pid for d in descendants):
                break
            await asyncio.sleep(0.1)
        assert any(d.pid == child_pid for d in descendants), "child was never seen in the descendant walk"

        await asyncio.to_thread(reap, [psutil.Process(proc.pid), *descendants])
        await proc.wait()
        assert not _alive(proc.pid)
        assert not _alive(child_pid)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(reap, [psutil.Process(proc.pid), *snapshot_descendants(proc.pid)])
        with contextlib.suppress(Exception):
            await proc.wait()


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a running, non-zombie process."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
