# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for cross-platform launch resolution of an agent's ACP-server command."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford.acp.launch import prepare_argv

_NT_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows shim resolution")


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
