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
