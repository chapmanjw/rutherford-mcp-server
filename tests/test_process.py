# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the real asyncio ProcessRunner, driven with the Python interpreter."""

from __future__ import annotations

import sys

from rutherford.domain.models import InvocationSpec
from rutherford.runtime.process import AsyncProcessRunner


def _spec(code: str, *, stdin: str | None = None, env: dict[str, str] | None = None) -> InvocationSpec:
    return InvocationSpec(argv=[sys.executable, "-c", code], stdin=stdin, env=env or {})


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
