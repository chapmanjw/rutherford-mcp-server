# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for cross-platform argv preparation (the Windows .cmd / shim handling)."""

from __future__ import annotations

import pytest

from rutherford.runtime.launch import merged_env, prepare_argv


def _which(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


def test_posix_resolves_and_passes_through() -> None:
    argv = prepare_argv(
        ["claude", "-p", "hi"],
        is_windows=False,
        which=_which({"claude": "/usr/local/bin/claude"}),
    )
    assert argv == ["/usr/local/bin/claude", "-p", "hi"]


def test_posix_unresolved_keeps_name() -> None:
    argv = prepare_argv(["mytool", "x"], is_windows=False, which=_which({}))
    assert argv == ["mytool", "x"]


def test_windows_exe_launches_directly() -> None:
    argv = prepare_argv(
        ["claude", "-p", "hi"],
        is_windows=True,
        which=_which({"claude": r"C:\bin\claude.EXE"}),
    )
    assert argv == [r"C:\bin\claude.EXE", "-p", "hi"]


def test_windows_cmd_shim_wrapped_in_cmd_exe() -> None:
    argv = prepare_argv(
        ["codex", "exec", "do it"],
        is_windows=True,
        which=_which({"codex": r"C:\npm\codex.cmd"}),
        comspec=r"C:\Windows\System32\cmd.exe",
    )
    assert argv == [r"C:\Windows\System32\cmd.exe", "/c", r"C:\npm\codex.cmd", "exec", "do it"]


def test_windows_extensionless_shim_wrapped_in_cmd_exe() -> None:
    # The real-world npm case: shutil.which resolves the bare shell shim, which CreateProcess
    # cannot execute; cmd.exe resolves it via PATHEXT.
    argv = prepare_argv(
        ["codex", "--version"],
        is_windows=True,
        which=_which({"codex": r"C:\npm\codex"}),
        comspec="cmd.exe",
    )
    assert argv == ["cmd.exe", "/c", r"C:\npm\codex", "--version"]


def test_windows_ps1_shim_wrapped_in_powershell() -> None:
    argv = prepare_argv(
        ["tool", "go"],
        is_windows=True,
        which=_which({"tool": r"C:\bin\tool.ps1"}),
        powershell="powershell",
    )
    assert argv == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r"C:\bin\tool.ps1", "go"]


def test_empty_argv_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        prepare_argv([])


def test_merged_env_overlays_on_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXISTING", "1")
    merged = merged_env({"NEW": "2"})
    assert merged is not None
    assert merged["EXISTING"] == "1"
    assert merged["NEW"] == "2"


def test_merged_env_none_when_empty() -> None:
    assert merged_env(None) is None
    assert merged_env({}) is None
