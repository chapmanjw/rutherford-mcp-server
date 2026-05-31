# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Path translation between Windows and WSL forms.

When an adapter's runtime differs from the host -- for example a Linux CLI reached from a Windows
host via WSL interop -- a working directory or file path must be translated between the Windows
form (``C:\\Users\\x``) and the WSL form (``/mnt/c/Users/x``). The manual ``/mnt/<drive>``
conversion is deterministic and the reliable fallback; ``wslpath`` is preferred when available, as
the spec notes.
"""

from __future__ import annotations

import re

from ..domain.enums import Runtime
from .platform import PlatformInfo

_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_MNT_RE = re.compile(r"^/mnt/([A-Za-z])/(.*)$")


def windows_to_wsl(path: str) -> str:
    """Convert a Windows path to its WSL ``/mnt/<drive>`` form.

    ``C:\\Users\\x`` becomes ``/mnt/c/Users/x``. A path that is already POSIX-style has its
    backslashes normalized to forward slashes; anything unrecognized is returned unchanged.
    """
    match = _WINDOWS_DRIVE_RE.match(path)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return path.replace("\\", "/")


def wsl_to_windows(path: str) -> str:
    """Convert a WSL ``/mnt/<drive>`` path to its Windows form.

    ``/mnt/c/Users/x`` becomes ``C:\\Users\\x``. Anything that is not an ``/mnt`` path is
    returned unchanged.
    """
    match = _WSL_MNT_RE.match(path)
    if match:
        drive = match.group(1).upper()
        rest = match.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return path


def translate_path(path: str, host: PlatformInfo, target_runtime: Runtime) -> str:
    """Translate ``path`` for a target runtime that differs from the host.

    On a Windows host targeting a WSL-interop (Linux) runtime, a Windows path is converted to its
    WSL form. On a WSL/Linux host targeting a native Windows runtime, a ``/mnt`` path is converted
    to its Windows form. When the runtime matches the host, the path is returned unchanged.
    """
    if target_runtime is Runtime.WSL_INTEROP and host.is_windows:
        return windows_to_wsl(path)
    if target_runtime is Runtime.NATIVE and host.is_wsl:
        return wsl_to_windows(path)
    return path
