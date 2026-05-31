# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for OS and WSL detection."""

from __future__ import annotations

from rutherford.runtime.platform import OSFamily, detect_platform


def test_windows() -> None:
    info = detect_platform(platform="win32", env={}, proc_version_reader=lambda: None)
    assert info.os_family is OSFamily.WINDOWS
    assert info.is_windows
    assert not info.is_wsl


def test_macos() -> None:
    info = detect_platform(platform="darwin", env={}, proc_version_reader=lambda: None)
    assert info.os_family is OSFamily.MACOS
    assert not info.is_wsl


def test_plain_linux_not_wsl() -> None:
    info = detect_platform(
        platform="linux",
        env={},
        proc_version_reader=lambda: "Linux version 6.8.0-generic (gcc ...)",
    )
    assert info.os_family is OSFamily.LINUX
    assert not info.is_wsl


def test_wsl_via_proc_version() -> None:
    info = detect_platform(
        platform="linux",
        env={},
        proc_version_reader=lambda: "Linux version 5.15.0-microsoft-standard-WSL2",
    )
    assert info.is_wsl


def test_wsl_via_env_distro() -> None:
    info = detect_platform(
        platform="linux",
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        proc_version_reader=lambda: None,
    )
    assert info.is_wsl
    assert info.wsl_distro == "Ubuntu"
