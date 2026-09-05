# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for cross-platform launch resolution of an agent's ACP-server command."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford.acp.launch import _on_disk_case, prepare_argv

_NT_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows shim resolution")


class _Entry:
    """The one attribute `_on_disk_case` reads off a scandir entry."""

    def __init__(self, name: str) -> None:
        self.name = name


def _fake_scandir(names: list[str]) -> Any:
    """A scandir stub with a CONTROLLED enumeration order.

    Real scandir order is filesystem- and inode-dependent, so the ordering hazard cannot be reproduced by
    creating files -- and on Windows the two-case-variants case cannot be created at all. Stubbing the order
    is the only way to pin the property under test on every platform.
    """

    class _Scan:
        def __enter__(self) -> list[_Entry]:
            return [_Entry(name) for name in names]

        def __exit__(self, *exc: object) -> None:
            return None

    def _scandir(_path: Any) -> _Scan:
        return _Scan()

    return _scandir


def test_prepare_argv_empty() -> None:
    assert prepare_argv(()) == []


def test_prepare_argv_resolves_a_real_executable() -> None:
    out = prepare_argv((Path(sys.executable).name, "-V"))
    assert out[-1] == "-V"
    assert Path(out[0]).is_absolute()


def test_prepare_argv_missing_returns_original() -> None:
    assert prepare_argv(("no-such-binary-xyz123", "a")) == ["no-such-binary-xyz123", "a"]


@_NT_ONLY
def test_prepare_argv_cmd_prefers_ps1_sibling(tmp_path: Path, monkeypatch: Any) -> None:
    cmd = tmp_path / "tool.cmd"
    cmd.write_text("@echo off", encoding="utf-8")
    (tmp_path / "tool.ps1").write_text("# ps", encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(cmd))
    out = prepare_argv(("tool", "--acp"))
    assert out[0] == "powershell.exe" and str(tmp_path / "tool.ps1") in out and out[-1] == "--acp"


@_NT_ONLY
def test_prepare_argv_cmd_without_ps1_uses_cmd_c(tmp_path: Path, monkeypatch: Any) -> None:
    cmd = tmp_path / "tool2.cmd"
    cmd.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(cmd))
    out = prepare_argv(("tool2", "x"))
    assert out[:2] == ["cmd.exe", "/c"] and out[-1] == "x"


@_NT_ONLY
def test_prepare_argv_ps1_uses_powershell(tmp_path: Path, monkeypatch: Any) -> None:
    ps1 = tmp_path / "tool3.ps1"
    ps1.write_text("# ps", encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(ps1))
    out = prepare_argv(("tool3",))
    assert out[0] == "powershell.exe" and str(ps1) in out


@_NT_ONLY
def test_npm_shim_resolves_to_native_exe(tmp_path: Path, monkeypatch: Any) -> None:
    exe = tmp_path / "node_modules" / "pkg" / "bin" / "tool.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("x", encoding="utf-8")
    cmd = tmp_path / "tool.cmd"
    cmd.write_text('@echo off\n"%dp0%\\node_modules\\pkg\\bin\\tool.exe" %*\n', encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(cmd))
    assert prepare_argv(("tool", "acp")) == [str(exe), "acp"]


@_NT_ONLY
def test_npm_shim_resolves_extensionless_node_script(tmp_path: Path, monkeypatch: Any) -> None:
    script = tmp_path / "node_modules" / "pkg" / "bin" / "tool"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_text("n", encoding="utf-8")
    cmd = tmp_path / "tool2.cmd"
    cmd.write_text(
        '@echo off\nSET "_prog=node"\n"%_prog%" "%dp0%\\node_modules\\pkg\\bin\\tool" %*\n', encoding="utf-8"
    )

    def which(name: str) -> str | None:
        return {"tool2": str(cmd), "node": str(node)}.get(name)

    monkeypatch.setattr("rutherford.acp.launch.shutil.which", which)
    assert prepare_argv(("tool2", "--acp")) == [str(node), str(script), "--acp"]


@_NT_ONLY
def test_npm_shim_missing_target_falls_back(tmp_path: Path, monkeypatch: Any) -> None:
    cmd = tmp_path / "tool4.cmd"
    cmd.write_text('@echo off\n"%dp0%\\node_modules\\pkg\\bin\\gone.exe" %*\n', encoding="utf-8")
    (tmp_path / "tool4.ps1").write_text("# ps", encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(cmd))
    assert prepare_argv(("tool4",))[0] == "powershell.exe"


@_NT_ONLY
def test_extensionless_npm_bin_resolves_via_cmd_sibling(tmp_path: Path, monkeypatch: Any) -> None:
    # shutil.which returns the EXTENSIONLESS npm bin (a Unix shell script Windows cannot exec, WinError 193);
    # prepare_argv must resolve it via the .cmd sibling shim to the real bundled .exe (codex-acp / claude case).
    exe = tmp_path / "node_modules" / "pkg" / "bin" / "tool5.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("x", encoding="utf-8")
    bare = tmp_path / "tool5"  # the extensionless shell-script bin npm also installs
    bare.write_text("#!/bin/sh\n", encoding="utf-8")
    cmd = tmp_path / "tool5.cmd"
    cmd.write_text('@echo off\n"%dp0%\\node_modules\\pkg\\bin\\tool5.exe" %*\n', encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(bare))
    assert prepare_argv(("tool5", "acp")) == [str(exe), "acp"]


@_NT_ONLY
def test_extensionless_bin_without_sibling_is_returned_unchanged(tmp_path: Path, monkeypatch: Any) -> None:
    # No .cmd/.ps1 sibling to resolve through: prepare_argv returns the command unchanged, so the spawn fails
    # naturally as ACP_SPAWN_FAILED rather than this function inventing a launch path.
    bare = tmp_path / "tool6"
    bare.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda name: str(bare))
    assert prepare_argv(("tool6", "acp")) == [str(bare), "acp"]


# --- on-disk filename case (uppercase PATHEXT) -------------------------------
#
# These are NOT gated to Windows. `_on_disk_case` runs on every platform on purpose -- macOS ships a
# case-insensitive APFS/HFS+ by default, where `which` likewise returns the caller's spelling rather than
# the dirent's -- so the behaviour is exercised on every CI cell instead of only the one that owns the bug.
#
# Every assertion below compares STRINGS. `Path("a.EXE") == Path("a.exe")` is True on Windows, so a Path
# comparison would stay green with the normalizer deleted, on the very platform that needs it most.


def test_prepare_argv_normalizes_synthesized_extension_case(tmp_path: Path, monkeypatch: Any) -> None:
    # shutil.which appends each PATHEXT entry VERBATIM, so an uppercase PATHEXT names a file whose dirent is
    # lowercase. A launcher that dispatches on its own argv[0] basename never recognizes that spelling.
    real = tmp_path / "kiro-cli.exe"
    real.write_bytes(b"")
    synthesized = str(tmp_path / "kiro-cli.EXE")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda _cmd: synthesized)

    argv = prepare_argv(("kiro-cli", "acp"))

    assert argv[0] == str(real)
    assert argv[0] != synthesized
    assert not argv[0].endswith(".EXE")
    assert argv[1:] == ["acp"]


def test_prepare_argv_leaves_a_correctly_spelled_name_untouched(tmp_path: Path, monkeypatch: Any) -> None:
    # Negative control. Without it the test above passes just as well against a function that lowercases
    # every path it is handed, which would corrupt a genuinely mixed-case binary name.
    real = tmp_path / "Kiro-Cli.exe"
    real.write_bytes(b"")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda _cmd: str(real))

    assert prepare_argv(("Kiro-Cli", "acp")) == [str(real), "acp"]


def test_prepare_argv_missing_parent_directory_returns_the_input(tmp_path: Path, monkeypatch: Any) -> None:
    # The scandir OSError guard. A PATH entry can vanish or deny enumeration between `which` and the spawn;
    # the honest fallback is the spelling we already have, never a crash in a previously working launch.
    gone = str(tmp_path / "vanished" / "tool.exe")
    monkeypatch.setattr("rutherford.acp.launch.shutil.which", lambda _cmd: gone)

    assert prepare_argv(("tool", "acp")) == [gone, "acp"]


def test_on_disk_case_prefers_an_exact_match_enumerated_last(monkeypatch: Any) -> None:
    # The ordering regression. scandir order is unspecified, so "return the first case-insensitive hit" makes
    # the answer depend on the filesystem's enumeration: with a case-variant listed first, the original sketch
    # rewrote a path that was already correct to a DIFFERENT file. Exact must win globally, not per-entry.
    monkeypatch.setattr("rutherford.acp.launch.os.scandir", _fake_scandir(["Foo.exe", "foo.exe"]))

    assert _on_disk_case(str(Path("bin") / "foo.exe")) == str(Path("bin") / "foo.exe")


def test_on_disk_case_refuses_to_choose_between_two_case_variants(monkeypatch: Any) -> None:
    # Case-sensitive filesystem, two dirents differing only in case, and NO exact match: either could be the
    # intended target, so the input is returned rather than guessing at a different binary.
    monkeypatch.setattr("rutherford.acp.launch.os.scandir", _fake_scandir(["Foo.exe", "FOO.exe"]))

    assert _on_disk_case(str(Path("bin") / "foo.exe")) == str(Path("bin") / "foo.exe")


def test_on_disk_case_corrects_a_single_case_variant(monkeypatch: Any) -> None:
    monkeypatch.setattr("rutherford.acp.launch.os.scandir", _fake_scandir(["other.txt", "kiro-cli.exe"]))

    assert _on_disk_case(str(Path("bin") / "kiro-cli.EXE")) == str(Path("bin") / "kiro-cli.exe")


def test_on_disk_case_does_not_dereference_a_link(tmp_path: Path) -> None:
    # Path.resolve()/os.path.realpath would also follow links, which pins a version-managed node shim to one
    # concrete install directory -- measured rewriting ...\nodejs\NODE.EXE to ...\node-v20.11.0\node.exe.
    # The parent directory component must survive untouched.
    real_dir = tmp_path / "node-v20"
    real_dir.mkdir()
    (real_dir / "node.exe").write_bytes(b"")
    link = tmp_path / "nodejs"
    try:
        link.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs Developer Mode / root on some hosts
        pytest.skip("symlink creation not permitted here")

    out = _on_disk_case(str(link / "node.EXE"))

    assert out == str(link / "node.exe")
    assert "node-v20" not in out
