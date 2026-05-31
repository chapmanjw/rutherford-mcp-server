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

import subprocess
import time
from typing import Protocol, runtime_checkable

from ..domain.models import ProcessResult
from .launch import merged_env, prepare_argv


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
            completed = subprocess.run(
                launch,
                capture_output=True,
                # Detach the child's stdin from ours. When Rutherford runs as a stdio MCP server
                # its stdin is the client's pipe; a probed CLI that reads stdin would otherwise
                # block on it (or steal protocol bytes).
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                env=merged_env(env),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                exit_code=None,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                duration_s=time.monotonic() - start,
                timed_out=True,
            )
        except (FileNotFoundError, OSError) as exc:
            return ProcessResult(
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration_s=time.monotonic() - start,
            )
        return ProcessResult(
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_s=time.monotonic() - start,
        )


def _as_text(value: str | bytes | None) -> str:
    """Coerce captured output (which may be bytes on a timeout) to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
