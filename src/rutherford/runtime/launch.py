# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Cross-platform argv preparation.

The no-shell-string rule holds everywhere: every invocation is a list. The wrinkle is Windows,
where several CLIs install as ``.cmd`` / ``.ps1`` shims (or extension-less npm shims) that
``CreateProcess`` cannot launch directly -- it raises ``WinError 193``. The sanctioned exception
(see the spec's Windows ``.cmd`` note) is to route those through ``cmd.exe /c`` (or PowerShell
for ``.ps1``), still passing every argument as a separate list element, never interpolating a
command string.

:func:`prepare_argv` resolves the executable honoring ``PATHEXT`` and applies that wrapping. It
is a pure function of its injected ``which`` and ``is_windows``, so it is unit-testable for both
platforms from a single host.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable

#: Extensions Windows can execute directly via CreateProcess.
_DIRECT_EXEC_SUFFIXES = (".exe", ".com")


def default_is_windows() -> bool:
    """Return whether the current host is Windows."""
    return sys.platform == "win32"


def prepare_argv(
    argv: list[str],
    *,
    is_windows: bool | None = None,
    which: Callable[[str], str | None] = shutil.which,
    comspec: str | None = None,
    powershell: str | None = None,
) -> list[str]:
    """Resolve ``argv[0]`` and wrap shims so the result launches without a shell.

    On non-Windows hosts the only change is resolving ``argv[0]`` to its absolute path when
    found on ``PATH``. On Windows, a resolved ``.cmd`` / ``.bat`` / extension-less shim is
    wrapped as ``[cmd.exe, /c, <resolved>, *rest]`` and a ``.ps1`` shim as
    ``[powershell, -NoProfile, -ExecutionPolicy, Bypass, -File, <resolved>, *rest]``. A real
    ``.exe`` (or any POSIX binary) is launched directly. ``argv`` is never mutated.

    Args:
        argv: The command and its arguments. ``argv[0]`` is the binary name or path.
        is_windows: Override host detection (for tests). Defaults to the real host.
        which: Executable resolver, honoring ``PATHEXT`` on Windows. Injectable for tests.
        comspec: The command interpreter. Defaults to ``%ComSpec%`` or ``cmd.exe``.
        powershell: The PowerShell executable. Defaults to ``pwsh`` then ``powershell``.

    Returns:
        A new argv list ready to hand to the process layer.

    Raises:
        ValueError: If ``argv`` is empty.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    if is_windows is None:
        is_windows = default_is_windows()

    resolved = which(argv[0]) or argv[0]
    rest = argv[1:]

    if not is_windows:
        return [resolved, *rest]

    lowered = resolved.lower()
    if lowered.endswith(_DIRECT_EXEC_SUFFIXES):
        return [resolved, *rest]
    if lowered.endswith(".ps1"):
        shell = powershell or which("pwsh") or which("powershell") or "powershell"
        return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved, *rest]
    # .cmd / .bat / extension-less npm shim / unknown: let the command interpreter resolve and
    # run it. Arguments stay as separate list elements -- no command string is assembled.
    interpreter = comspec or os.environ.get("COMSPEC", "cmd.exe")
    return [interpreter, "/c", resolved, *rest]


def merged_env(overlay: dict[str, str] | None) -> dict[str, str] | None:
    """Overlay ``overlay`` on the inherited process environment.

    Returns ``None`` when there is nothing to overlay, so the child simply inherits the parent
    environment unchanged.
    """
    if not overlay:
        return None
    return {**os.environ, **overlay}
