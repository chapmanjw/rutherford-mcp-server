# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for Windows <-> WSL path translation."""

from __future__ import annotations

from rutherford.domain.enums import Runtime
from rutherford.runtime.paths import translate_path, windows_to_wsl, wsl_to_windows
from rutherford.runtime.platform import OSFamily, PlatformInfo


def test_windows_to_wsl() -> None:
    assert windows_to_wsl(r"C:\Users\chapm\Projects") == "/mnt/c/Users/chapm/Projects"
    assert windows_to_wsl("D:/data/file.txt") == "/mnt/d/data/file.txt"
    assert windows_to_wsl("C:\\") == "/mnt/c"  # drive root: a single trailing backslash


def test_windows_to_wsl_passthrough_posix() -> None:
    assert windows_to_wsl("/already/posix") == "/already/posix"


def test_wsl_to_windows() -> None:
    assert wsl_to_windows("/mnt/c/Users/chapm") == r"C:\Users\chapm"
    assert wsl_to_windows("/mnt/d/data/file.txt") == r"D:\data\file.txt"


def test_wsl_to_windows_passthrough_non_mnt() -> None:
    assert wsl_to_windows("/home/chapm") == "/home/chapm"


def test_translate_windows_host_to_wsl_runtime() -> None:
    host = PlatformInfo(os_family=OSFamily.WINDOWS, is_wsl=False)
    assert translate_path(r"C:\work", host, Runtime.WSL_INTEROP) == "/mnt/c/work"


def test_translate_wsl_host_to_native_windows_runtime() -> None:
    host = PlatformInfo(os_family=OSFamily.LINUX, is_wsl=True, wsl_distro="Ubuntu")
    assert translate_path("/mnt/c/work", host, Runtime.NATIVE) == r"C:\work"


def test_translate_noop_when_runtime_matches_host() -> None:
    host = PlatformInfo(os_family=OSFamily.WINDOWS, is_wsl=False)
    assert translate_path(r"C:\work", host, Runtime.NATIVE) == r"C:\work"
