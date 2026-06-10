# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Synchronous command probing for adapter metadata.

``detect`` / ``check_auth`` / ``available_models`` need quick, read-only metadata calls --
``which`` the binary, read its ``--version``, ask an auth-status command, list models. Those are
distinct from the async, tree-killing orchestration path (``ProcessRunner``): they are short,
synchronous, and never mutate. The :class:`CommandProbe` interface lets adapters take that
capability by injection, so every adapter's metadata methods are unit-testable with a
:class:`~tests.fakes` fake and no real CLI. :class:`SystemProbe` is the real implementation.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from typing import Protocol, runtime_checkable

from ..domain.models import ProcessResult
from .launch import merged_env, prepare_argv
from .process import kill_process_tree


@runtime_checkable
class CommandProbe(Protocol):
    """A read-only command runner used by adapter metadata methods."""

    def which(self, name: str) -> str | None:
        """Return the resolved path of ``name`` on ``PATH``, or ``None`` if absent."""
        ...

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Run ``argv`` to completion and capture its output. Never raises on a CLI failure."""
        ...


class SystemProbe:
    """The real :class:`CommandProbe`, backed by :mod:`subprocess`.

    Resolves and wraps the command with :func:`~rutherford.runtime.launch.prepare_argv` so
    Windows ``.cmd``/``.ps1`` shims run correctly, captures stdout/stderr as text, and turns a
    timeout or a missing binary into a structured :class:`ProcessResult` rather than an
    exception -- these are normal outcomes for a probe.
    """

    def which(self, name: str) -> str | None:
        from shutil import which as _which

        return _which(name)

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        launch = prepare_argv(argv)
        start = time.monotonic()
        try:
            # Decode as UTF-8 with replacement rather than the platform locale (cp1252 on
            # Windows), which raises UnicodeDecodeError on a CLI that emits UTF-8 bytes.
            # Popen + communicate (not subprocess.run) so the timeout path below can tree-kill.
            process = subprocess.Popen(
                launch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Detach the child's stdin from ours. When Rutherford runs as a stdio MCP server
                # its stdin is the client's pipe; a probed CLI that reads stdin would otherwise
                # block on it (or steal protocol bytes).
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                env=merged_env(env),
            )
        except (FileNotFoundError, OSError) as exc:
            return ProcessResult(
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration_s=time.monotonic() - start,
            )
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # subprocess.run's own timeout kills only the direct child. A probed command behind a
            # Windows cmd.exe/.cmd shim (or a node/python wrapper) forks the real CLI, which would
            # outlive the timeout; use the same tree-kill policy as the async runner.
            kill_process_tree(process.pid)
            stdout, stderr = "", ""
            with contextlib.suppress(Exception):  # drain whatever arrived and reap the child
                stdout, stderr = process.communicate(timeout=5)
            return ProcessResult(
                exit_code=None,
                stdout=_as_text(stdout),
                stderr=_as_text(stderr),
                duration_s=time.monotonic() - start,
                timed_out=True,
            )
        return ProcessResult(
            exit_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=time.monotonic() - start,
        )


def _as_text(value: str | bytes | None) -> str:
    """Coerce captured output (which may be bytes on a timeout) to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
