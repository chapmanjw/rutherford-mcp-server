# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Operating-system and WSL detection.

Rutherford must run natively on Windows, macOS, and Linux, including Ubuntu under WSL. WSL is
detected by the Microsoft signature in ``/proc/version`` or the ``WSL_DISTRO_NAME`` environment
variable. :func:`detect_platform` takes its inputs by injection so both WSL and non-WSL hosts are
unit-testable from a single machine.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class OSFamily(StrEnum):
    """The host operating-system family."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """A snapshot of the host platform."""

    os_family: OSFamily
    is_wsl: bool
    wsl_distro: str | None = None

    @property
    def is_windows(self) -> bool:
        """Whether the host is native Windows."""
        return self.os_family is OSFamily.WINDOWS


def _os_family_from(platform: str) -> OSFamily:
    if platform == "win32":
        return OSFamily.WINDOWS
    if platform == "darwin":
        return OSFamily.MACOS
    return OSFamily.LINUX


def _default_proc_version_reader() -> str | None:
    """Read ``/proc/version``, or return ``None`` when it is absent (non-Linux hosts)."""
    try:
        return Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def detect_platform(
    *,
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    proc_version_reader: Callable[[], str | None] = _default_proc_version_reader,
) -> PlatformInfo:
    """Detect the host OS family and whether it is WSL.

    Args:
        platform: The platform string (defaults to :data:`sys.platform`).
        env: The environment to inspect (defaults to :data:`os.environ`).
        proc_version_reader: Returns ``/proc/version`` contents, or ``None``. Injectable for tests.
    """
    environ = os.environ if env is None else env
    family = _os_family_from(platform)

    distro = environ.get("WSL_DISTRO_NAME")
    is_wsl = False
    if family is OSFamily.LINUX:
        if distro:
            is_wsl = True
        else:
            proc = proc_version_reader() or ""
            lowered = proc.lower()
            is_wsl = "microsoft" in lowered or "wsl" in lowered
    return PlatformInfo(os_family=family, is_wsl=is_wsl, wsl_distro=distro)
