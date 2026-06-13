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


def test_windows_extensionless_shim_prefers_ps1_sibling() -> None:
    # The npm trio: shutil.which resolves the extension-less shell shim, but a .ps1 sibling exists in
    # the SAME directory. Prefer the .ps1 (via powershell -File) so a multi-line argv element is not
    # truncated by cmd.exe and piped stdin is forwarded. The sibling is looked up by full path.
    argv = prepare_argv(
        ["cn", "-p", "line one\nline two"],
        is_windows=True,
        which=_which({"cn": r"C:\npm\cn", r"C:\npm\cn.ps1": r"C:\npm\cn.ps1"}),
        powershell="powershell",
    )
    assert argv == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        r"C:\npm\cn.ps1",
        "-p",
        "line one\nline two",
    ]


def test_windows_cmd_shim_prefers_ps1_sibling() -> None:
    # Even when which resolves the .cmd, a .ps1 sibling in the same directory wins for the same reason.
    argv = prepare_argv(
        ["kilo", "run", "do it"],
        is_windows=True,
        which=_which({"kilo": r"C:\npm\kilo.cmd", r"C:\npm\kilo.ps1": r"C:\npm\kilo.ps1"}),
        powershell="powershell",
    )
    assert argv == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        r"C:\npm\kilo.ps1",
        "run",
        "do it",
    ]


def test_windows_shim_does_not_use_a_ps1_from_a_different_directory() -> None:
    # PATH-confusion guard: a same-named .ps1 in a DIFFERENT directory must never be substituted for the
    # .cmd that which() resolved. The sibling is resolved by the resolved shim's full path, so a stray
    # C:\B\codex.ps1 (elsewhere on PATH) is ignored and the resolved C:\A\codex.cmd runs via cmd.exe.
    argv = prepare_argv(
        ["codex", "exec"],
        is_windows=True,
        which=_which({"codex": r"C:\A\codex.cmd", r"C:\B\codex.ps1": r"C:\B\codex.ps1"}),
        comspec="cmd.exe",
    )
    assert argv == ["cmd.exe", "/c", r"C:\A\codex.cmd", "exec"]


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
