# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the synchronous command probe, driven with the Python interpreter."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
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
    real = subprocess.Popen

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("rutherford.runtime.probe.subprocess.Popen", spy)
    SystemProbe().run([sys.executable, "-c", "pass"])
    assert captured["stdin"] is subprocess.DEVNULL


def test_probe_timeout_kills_the_wrapped_process_tree(tmp_path: Path) -> None:
    # subprocess.run's own timeout reaps only the direct child. A probed command behind a shim or
    # wrapper forks the real CLI; the probe's timeout path must tree-kill so that grandchild does
    # not outlive the (cached) timeout verdict.
    code = (
        "import subprocess, sys, time, os\n"
        "from pathlib import Path\n"
        f"out = Path({str(tmp_path)!r})\n"
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
        '(out / "parent.pid").write_text(str(os.getpid()))\n'
        '(out / "child.pid").write_text(str(child.pid))\n'
        "time.sleep(60)\n"
    )
    result = SystemProbe().run([sys.executable, "-c", code], timeout_s=3.0)
    assert result.timed_out
    parent_pid = int((tmp_path / "parent.pid").read_text())
    child_pid = int((tmp_path / "child.pid").read_text())
    for pid in (parent_pid, child_pid):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and psutil.pid_exists(pid):
            time.sleep(0.05)
        assert not psutil.pid_exists(pid), f"pid {pid} survived the probe timeout"


def test_probe_tolerates_invalid_utf8_output() -> None:
    # Pins the errors="replace" decode across the Popen migration: a CLI emitting non-UTF-8 bytes
    # must yield replacement characters, never raise UnicodeDecodeError out of the probe.
    code = "import sys; sys.stdout.buffer.write(b'ok ' + bytes([255, 254]) + b' end'); sys.stdout.buffer.flush()"
    result = SystemProbe().run([sys.executable, "-c", code], timeout_s=30.0)
    assert result.exit_code == 0
    assert "ok" in result.stdout and "end" in result.stdout
    assert "�" in result.stdout  # the invalid bytes became replacement chars
