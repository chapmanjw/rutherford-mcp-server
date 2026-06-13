# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``ProcessRunner`` interface and its real asyncio implementation.

``ProcessRunner`` abstracts subprocess execution so the orchestration core can be driven by a
``FakeProcessRunner`` in tests, and so every cross-platform concern -- argv resolution, the
no-shell rule, the timeout, and process-tree termination -- lives in exactly one place.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..domain.models import InvocationSpec, ProcessResult
from .launch import merged_env, prepare_argv


@runtime_checkable
class ProcessRunner(Protocol):
    """Runs a resolved :class:`InvocationSpec` to completion under a timeout."""

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Execute ``spec``, enforce ``timeout_s``, and return the raw outcome.

        A timeout or a non-zero exit are normal outcomes returned as a :class:`ProcessResult` --
        not raised. A *spawn* failure (missing binary, exec error) raises ``OSError``; the
        delegation service normalizes that to a structured ``SPAWN_FAILED`` result, so a direct
        caller of this interface must handle it. On timeout or cancellation the whole process
        tree is killed.

        ``on_progress`` receives stderr lines as they arrive; ``on_stdout`` (F8a, decision 2-F/2-G)
        receives stdout lines, so a caller can tee the answer stream into a job artifact and/or
        accumulate it to harvest a partial answer if a time budget cuts the run. On a timeout the
        accumulated stdout is returned on ``ProcessResult.partial``; on cancellation the runner
        re-raises (its contract), so the cancel-path partial is whatever ``on_stdout`` already saw.
        """
        ...


class AsyncProcessRunner:
    """The real :class:`ProcessRunner`: asyncio subprocess, argv list, process-tree kill.

    Uses :func:`~rutherford.runtime.launch.prepare_argv` so Windows ``.cmd``/``.ps1`` shims run
    correctly, never assembles a shell string, streams stderr lines to ``on_progress`` as they
    arrive, enforces the timeout, and on timeout or cancellation terminates the entire process
    tree with :mod:`psutil` (these agents spawn children, so killing only the direct child would
    orphan them).
    """

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        launch = prepare_argv(spec.argv)
        start = time.monotonic()
        # When stdin is not supplied, detach the child from our stdin (DEVNULL) rather than
        # inheriting it. Under a stdio MCP client our stdin is the client's pipe, and a spawned
        # CLI that reads stdin would block on it or consume protocol bytes.
        process = await asyncio.create_subprocess_exec(
            *launch,
            stdin=asyncio.subprocess.PIPE if spec.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=merged_env(spec.env),
        )
        stdin_bytes = spec.stdin.encode("utf-8") if spec.stdin is not None else None
        # The accumulator lives in run()'s frame so partial stdout survives a cancellation of
        # _communicate (the wait_for timeout cancels that coroutine; its locals would be lost).
        stdout_acc = bytearray()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                self._communicate(process, stdin_bytes, on_progress, on_stdout, stdout_acc),
                timeout=timeout_s,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            await asyncio.to_thread(kill_process_tree, process.pid)
            duration = time.monotonic() - start
            if isinstance(exc, asyncio.CancelledError):
                raise
            return ProcessResult(
                exit_code=None,
                stdout="",
                stderr=f"timed out after {timeout_s:.0f}s",
                duration_s=duration,
                timed_out=True,
                # Preserve what the child wrote before the deadline so a time-budget harvester can
                # surface a candidate answer instead of throwing the work away (decision 2-F).
                partial=bytes(stdout_acc).decode("utf-8", errors="replace") or None,
            )
        except BaseException:
            # Any other escape from the wait (an OSError from the stdin feed beyond the tolerated
            # pipe errors, a raising on_progress callback) must not leak a live child: kill the
            # tree (best-effort, a no-op on a dead pid) and let the original exception propagate.
            await asyncio.to_thread(kill_process_tree, process.pid)
            raise
        return ProcessResult(
            exit_code=process.returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_s=time.monotonic() - start,
        )

    @staticmethod
    async def _communicate(
        process: asyncio.subprocess.Process,
        stdin_bytes: bytes | None,
        on_progress: Callable[[str], None] | None,
        on_stdout: Callable[[str], None] | None,
        stdout_acc: bytearray,
    ) -> tuple[bytes, bytes]:
        """Drain stdout/stderr while feeding stdin, teeing each stream's lines to its callback.

        The drains MUST be running before stdin is written: a child that fills its output pipe
        before consuming a large prompt would otherwise deadlock against a parent blocked in
        ``stdin.drain()`` on a full stdin pipe -- surfacing as a false timeout on a healthy CLI.
        (This is the ordering ``Process.communicate()`` itself uses; reimplemented to tee stderr to
        ``on_progress`` and stdout to ``on_stdout`` as they arrive, and to accumulate stdout into
        ``stdout_acc`` so a partial answer survives a deadline cut.)
        """
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_acc = bytearray()

        async def feed_stdin() -> None:
            if stdin_bytes is None or process.stdin is None:
                return
            try:
                process.stdin.write(stdin_bytes)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the child exited (or closed stdin) before reading everything; its output decides
            finally:
                process.stdin.close()

        await asyncio.gather(
            _drain_stream(process.stdout, stdout_acc, on_stdout),
            _drain_stream(process.stderr, stderr_acc, on_progress),
            feed_stdin(),
        )
        await process.wait()
        return bytes(stdout_acc), bytes(stderr_acc)


async def _drain_stream(
    reader: asyncio.StreamReader,
    acc: bytearray,
    sink: Callable[[str], None] | None,
) -> None:
    """Read ``reader`` in bounded chunks into ``acc``, teeing complete lines to ``sink`` as they arrive.

    Bounded ``read(65536)`` rather than ``readline()``: a single line longer than the ``StreamReader``
    limit (64 KiB) makes ``readline()`` raise ``ValueError``, which would escape the runner as a crash.
    Lines are re-split only to feed ``sink``; a pathological never-newline stream flushes oversized
    chunks rather than buffering unbounded. ``acc`` always receives every byte (so a partial answer
    survives a cut); ``sink`` is optional. Shared by the stdout and stderr drains so both behave
    identically.

    The trailing not-yet-newline-terminated ``pending`` is flushed to ``sink`` in a ``finally``, so it
    reaches the sink on EOF AND on cancellation (F8a, 2-F): a budget cut delivers ``CancelledError`` at
    the ``read`` with no EOF, and without the finally the last incomplete line a voice streamed before
    the cut would be lost to the panel's partial accumulator (``acc`` still has the bytes for the
    timeout path, but a cut re-raises rather than returning ``acc``).
    """
    pending = b""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            acc.extend(chunk)
            if sink is not None:
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    sink(line.decode("utf-8", errors="replace").rstrip("\r"))
                if len(pending) > 65536:  # no newline in sight; flush rather than buffer unbounded
                    sink(pending.decode("utf-8", errors="replace"))
                    pending = b""
    finally:
        if sink is not None and pending:
            sink(pending.decode("utf-8", errors="replace").rstrip("\r"))


def kill_process_tree(pid: int, timeout_s: float = 3.0) -> None:
    """Terminate ``pid`` and all descendants. Best-effort; never raises.

    Public within the runtime layer: the async runner's timeout/cancel path and the synchronous
    probe's timeout path share this one tree-kill policy, so a Windows ``cmd.exe`` shim that
    forked the real CLI is reaped the same way on both paths.

    Uses ``terminate()`` then ``kill()`` (not raw signals) so it works identically on POSIX and
    Windows -- the official psutil recipe, plus a second ``wait_procs`` after the kill pass so
    cleanup is actually complete (not merely signalled) when this returns.

    Residual risk, accepted: if the direct child has *already exited* by the time this runs, its
    still-live descendants have been reparented and are no longer discoverable from ``pid`` --
    they are not killed. Closing that window needs OS process groups / job objects, which is a
    larger change than this best-effort path warrants today.
    """
    import contextlib

    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a hard dependency
        return
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return  # parent already gone; reparented descendants are undiscoverable (see docstring)
    procs.append(parent)
    for proc in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout_s)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.kill()
    if alive:  # SIGKILL is not catchable, but wait so the tree is reaped before we report done
        psutil.wait_procs(alive, timeout=timeout_s)
