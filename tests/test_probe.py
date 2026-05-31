# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the synchronous command probe, driven with the Python interpreter."""

from __future__ import annotations

import subprocess
import sys

import pytest

from rutherford.runtime.probe import SystemProbe


def test_probe_runs_and_captures() -> None:
    probe = SystemProbe()
    result = probe.run([sys.executable, "-c", "print('probe-ok')"])
    assert result.exit_code == 0
    assert "probe-ok" in result.stdout


def test_probe_nonzero_exit() -> None:
    probe = SystemProbe()
    result = probe.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.exit_code == 3


def test_probe_missing_binary_is_structured() -> None:
    # A missing binary is a normal, structured outcome, not an exception: exit None on POSIX
    # (FileNotFoundError) or a non-zero code on Windows (cmd.exe reports "not recognized").
    probe = SystemProbe()
    result = probe.run(["this-binary-does-not-exist-rutherford"])
    assert result.exit_code != 0
    assert result.stderr
    assert not result.timed_out


def test_probe_timeout() -> None:
    probe = SystemProbe()
    result = probe.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
    assert result.timed_out
    assert result.exit_code is None


def test_probe_which_known_and_unknown() -> None:
    probe = SystemProbe()
    assert probe.which("this-binary-does-not-exist-rutherford") is None


def test_probe_detaches_child_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: a metadata probe must not inherit the server's stdin (the MCP pipe under a
    # stdio client), or a CLI that reads stdin would hang.
    captured: dict[str, object] = {}
    real = subprocess.run

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("rutherford.runtime.probe.subprocess.run", spy)
    SystemProbe().run([sys.executable, "-c", "pass"])
    assert captured["stdin"] is subprocess.DEVNULL
