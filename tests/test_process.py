# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the real asyncio ProcessRunner, driven with the Python interpreter."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import psutil
import pytest

from rutherford.domain.models import InvocationSpec
from rutherford.runtime.process import AsyncProcessRunner


def _spec(code: str, *, stdin: str | None = None, env: dict[str, str] | None = None) -> InvocationSpec:
    return InvocationSpec(argv=[sys.executable, "-c", code], stdin=stdin, env=env or {})


#: A parent that spawns a long-sleeping grandchild, records both PIDs, then sleeps itself --
#: the shape that exists to falsify the process-TREE kill (a single-PID kill leaves the
#: grandchild running). PID files land in the directory passed as argv[1] via -c globals.
_TREE_CODE = """
import subprocess, sys, time, os
from pathlib import Path
out = Path({outdir!r})
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(out / "parent.pid").write_text(str(os.getpid()))
(out / "child.pid").write_text(str(child.pid))
time.sleep(60)
"""


def _wait_for_pid_files(outdir: Path, timeout_s: float = 20.0) -> tuple[int, int]:
    """Block until the tree script has written both PID files, then return (parent, child)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        parent, child = outdir / "parent.pid", outdir / "child.pid"
        if parent.is_file() and child.is_file():
            parent_text, child_text = parent.read_text(), child.read_text()
            if parent_text and child_text:
                return int(parent_text), int(child_text)
        time.sleep(0.05)
    raise AssertionError("tree script never wrote its PID files")


def _assert_eventually_dead(pid: int, timeout_s: float = 10.0) -> None:
    """Assert ``pid`` exits within ``timeout_s`` (kill delivery is asynchronous on Windows)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        try:  # a reaped-but-listed zombie counts as dead
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} is still alive after the kill")


async def test_runs_and_captures_stdout() -> None:
    runner = AsyncProcessRunner()
    result = await runner.run(_spec("print('hello world')"), timeout_s=30)
    assert result.exit_code == 0
    assert "hello world" in result.stdout
    assert not result.timed_out
    assert result.duration_s >= 0


async def test_nonzero_exit_is_captured_not_raised() -> None:
    runner = AsyncProcessRunner()
    result = await runner.run(_spec("import sys; sys.exit(7)"), timeout_s=30)
    assert result.exit_code == 7
    assert not result.timed_out


async def test_timeout_sets_flag_and_kills() -> None:
    runner = AsyncProcessRunner()
    result = await runner.run(_spec("import time; time.sleep(30)"), timeout_s=1)
    assert result.timed_out
    assert result.exit_code is None


async def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    # The module's core promise: on timeout the DESCENDANTS die too, not just the direct child.
    # Replacing kill_process_tree with a single-PID kill (or a no-op) must fail this test.
    runner = AsyncProcessRunner()
    result = await runner.run(_spec(_TREE_CODE.format(outdir=str(tmp_path))), timeout_s=3)
    assert result.timed_out
    parent_pid, child_pid = _wait_for_pid_files(tmp_path)
    _assert_eventually_dead(parent_pid)
    _assert_eventually_dead(child_pid)  # the grandchild is the part a naive kill leaves orphaned


async def test_a_raising_on_progress_callback_kills_the_tree_and_reraises(tmp_path: Path) -> None:
    # Regression (F2): only timeout/cancellation used to trigger the tree kill; any other exception
    # escaping the wait -- here a raising on_progress callback -- leaked a live child tree. The
    # catch-all cleanup must kill the whole tree AND re-raise the original exception.
    tree_with_progress = """
import subprocess, sys, time, os
from pathlib import Path
out = Path({outdir!r})
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(out / "parent.pid").write_text(str(os.getpid()))
(out / "child.pid").write_text(str(child.pid))
sys.stderr.write("tick\\n")
sys.stderr.flush()
time.sleep(60)
"""
    runner = AsyncProcessRunner()

    def explode(_line: str) -> None:
        raise RuntimeError("progress callback exploded")

    with pytest.raises(RuntimeError, match="progress callback exploded"):
        await runner.run(_spec(tree_with_progress.format(outdir=str(tmp_path))), timeout_s=60, on_progress=explode)
    parent_pid, child_pid = _wait_for_pid_files(tmp_path)
    _assert_eventually_dead(parent_pid)
    _assert_eventually_dead(child_pid)


async def test_cancellation_kills_the_tree_and_reraises(tmp_path: Path) -> None:
    # The other half of the contract: cancelling an in-flight run (an aborted consensus/debate)
    # kills the tree AND re-raises CancelledError -- swallowing it would corrupt task semantics.
    runner = AsyncProcessRunner()
    task = asyncio.ensure_future(runner.run(_spec(_TREE_CODE.format(outdir=str(tmp_path))), timeout_s=60))
    parent_pid, child_pid = await asyncio.to_thread(_wait_for_pid_files, tmp_path)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_eventually_dead(parent_pid)
    _assert_eventually_dead(child_pid)


async def test_oversized_single_stderr_line_does_not_crash_the_runner() -> None:
    # Regression: a single stderr line past the 64 KiB StreamReader default made readline() raise
    # ValueError, which escaped the runner (and delegation's except OSError) as an unhandled crash.
    runner = AsyncProcessRunner()
    code = "import sys; sys.stderr.write('x' * 200_000); sys.stderr.flush(); print('answer')"
    result = await runner.run(_spec(code), timeout_s=30, on_progress=lambda _line: None)
    assert result.exit_code == 0
    assert "answer" in result.stdout
    assert result.stderr.count("x") == 200_000  # the oversized line is captured whole, not truncated


async def test_large_stdin_does_not_deadlock_against_early_output() -> None:
    # Regression: stdin used to be written and drained BEFORE the output drains started. A child
    # that fills its stdout pipe before reading stdin then deadlocks both processes, surfacing as
    # a false timeout. The child here writes ~256 KiB first, then echoes a large stdin's length.
    runner = AsyncProcessRunner()
    code = (
        "import sys; sys.stdout.write('y' * 262_144); sys.stdout.flush(); "
        "data = sys.stdin.read(); print(); print(len(data))"
    )
    result = await runner.run(_spec(code, stdin="z" * 262_144), timeout_s=30)
    assert result.exit_code == 0
    assert not result.timed_out
    assert result.stdout.rstrip().endswith("262144")  # the full stdin arrived after the early flood


async def test_child_exiting_before_reading_stdin_is_not_a_crash() -> None:
    # A child that never consumes its (large) stdin closes the pipe; the BrokenPipeError from the
    # feed must be tolerated, with the child's own output/exit deciding the outcome.
    runner = AsyncProcessRunner()
    result = await runner.run(_spec("print('early exit')", stdin="q" * 262_144), timeout_s=30)
    assert result.exit_code == 0
    assert "early exit" in result.stdout


async def test_stdin_is_fed() -> None:
    runner = AsyncProcessRunner()
    code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    result = await runner.run(_spec(code, stdin="hello"), timeout_s=30)
    assert result.exit_code == 0
    assert "HELLO" in result.stdout


async def test_on_progress_receives_stderr_lines() -> None:
    runner = AsyncProcessRunner()
    seen: list[str] = []
    code = "import sys; sys.stderr.write('progress one\\n'); sys.stderr.flush(); print('done')"
    result = await runner.run(_spec(code), timeout_s=30, on_progress=seen.append)
    assert "done" in result.stdout
    assert any("progress one" in line for line in seen)


async def test_env_overlay_passed_to_child() -> None:
    runner = AsyncProcessRunner()
    code = "import os; print(os.environ.get('RUTHERFORD_DEPTH', 'unset'))"
    result = await runner.run(_spec(code, env={"RUTHERFORD_DEPTH": "2"}), timeout_s=30)
    assert result.stdout.strip() == "2"


async def test_child_stdin_detached_when_not_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: under a stdio MCP client the server's stdin is the client's pipe; a spawned CLI
    # must not inherit it (it would block reading the pipe). DEVNULL detaches it.
    captured: dict[str, object] = {}
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await AsyncProcessRunner().run(_spec("pass"), timeout_s=30)
    assert captured["stdin"] is asyncio.subprocess.DEVNULL


async def test_child_stdin_is_pipe_when_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await AsyncProcessRunner().run(_spec("import sys; sys.stdin.read()", stdin="hi"), timeout_s=30)
    assert captured["stdin"] is asyncio.subprocess.PIPE


class _StubProc:
    """A psutil-process stand-in recording terminate/kill, for driving kill_process_tree's logic."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_kill_process_tree_waits_again_after_the_kill_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # "Returned" must mean REAPED, not merely signalled: a child that survives terminate() gets
    # kill() AND a second wait_procs. Stub-driven because a portable terminate-ignoring real child
    # does not exist (SIGTERM handlers vs Windows TerminateProcess) -- without the stub this branch
    # is dead code under the suite, exactly what the gap audit flagged.
    import psutil as psutil_mod

    from rutherford.runtime.process import kill_process_tree

    survivor = _StubProc()
    parent = _StubProc()
    parent.children = lambda recursive=False: [survivor]  # type: ignore[attr-defined]
    monkeypatch.setattr(psutil_mod, "Process", lambda pid: parent)

    wait_calls: list[list[object]] = []

    def fake_wait_procs(procs, timeout=None):
        wait_calls.append(list(procs))
        if len(wait_calls) == 1:
            return [], [survivor]  # the child ignored terminate()
        return list(procs), []

    monkeypatch.setattr(psutil_mod, "wait_procs", fake_wait_procs)
    kill_process_tree(12345)
    assert survivor.terminated and survivor.killed
    assert len(wait_calls) == 2  # the post-kill reap actually ran
    assert wait_calls[1] == [survivor]


def test_kill_process_tree_tolerates_the_parent_vanishing_before_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The race guard: Process(pid) succeeds but the parent exits before children() -- the
    # NoSuchProcess must be swallowed (best-effort, never raises), not escape the kill path.
    import psutil as psutil_mod

    from rutherford.runtime.process import kill_process_tree

    class _VanishingParent:
        def children(self, recursive: bool = False):
            raise psutil_mod.NoSuchProcess(12345)

    monkeypatch.setattr(psutil_mod, "Process", lambda pid: _VanishingParent())
    kill_process_tree(12345)  # must not raise
