# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Cross-platform argv preparation.

The no-shell-string rule holds everywhere: every invocation is a list. The wrinkle is Windows,
where several CLIs install as ``.cmd`` / ``.ps1`` shims (or extension-less npm shims) that
``CreateProcess`` cannot launch directly -- it raises ``WinError 193``. The sanctioned exception
(see the spec's Windows ``.cmd`` note) is to route those through ``cmd.exe /c`` (or PowerShell
for ``.ps1``), still passing every argument as a separate list element, never interpolating a
command string.

One ``cmd.exe`` hazard forces a preference between the two routes: ``cmd.exe`` **truncates an
argument at its first newline**, so a multi-line argv element (a role preamble folded into the
prompt) loses everything after line one, and it does not forward a programmatic stdin pipe to some
node shims. The ``.ps1`` route has neither problem -- PowerShell preserves a multi-line argument and
the npm ``.ps1`` shim forwards piped stdin (``$MyInvocation.ExpectingInput``). npm installs all three
shims per command (an extension-less shell script, a ``.cmd``, and a ``.ps1``) and ``shutil.which``
resolves the extension-less one, so :func:`prepare_argv` prefers a sibling ``.ps1`` over ``cmd.exe``
when one exists, falling back to ``cmd.exe`` only for a ``.cmd``/``.bat`` with no ``.ps1`` companion.

:func:`prepare_argv` resolves the executable honoring ``PATHEXT`` and applies that wrapping. It
is a pure function of its injected ``which`` and ``is_windows``, so it is unit-testable for both
platforms from a single host.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import PurePath

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
    found on ``PATH``. On Windows, a ``.ps1`` shim (resolved directly or found as the sibling of a
    ``.cmd`` / extension-less shim) is wrapped as
    ``[powershell, -NoProfile, -ExecutionPolicy, Bypass, -File, <ps1>, *rest]`` -- preferred because
    it preserves a multi-line argument and forwards piped stdin; a ``.cmd`` / ``.bat`` / extension-less
    shim with no ``.ps1`` companion falls back to ``[cmd.exe, /c, <resolved>, *rest]``. A real
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

    resolved_path = which(argv[0])
    resolved = resolved_path or argv[0]
    rest = argv[1:]

    if not is_windows:
        return [resolved, *rest]

    lowered = resolved.lower()
    if lowered.endswith(_DIRECT_EXEC_SUFFIXES):
        return [resolved, *rest]
    if lowered.endswith(".ps1"):
        return _powershell_file(resolved, rest, powershell, which)
    # A .cmd / .bat / extension-less npm shim. Prefer a sibling .ps1 (npm installs all three) so a
    # multi-line argv element survives and piped stdin is forwarded -- cmd.exe truncates an argument at
    # its first newline. The sibling is resolved by the resolved shim's FULL path (same directory), not by
    # bare name: a bare-name lookup would search PATH and could substitute a same-named .ps1 from a
    # different directory for the .cmd that ``which`` actually chose (a PATH-confusion hazard, the worse
    # for launching under -ExecutionPolicy Bypass). Only attempted when argv[0] itself resolved on PATH.
    # Fall back to cmd.exe when no sibling .ps1 exists; arguments stay separate list elements either way.
    if resolved_path is not None:
        ps1 = which(str(PurePath(resolved_path).with_suffix(".ps1")))
        if ps1 is not None:
            return _powershell_file(ps1, rest, powershell, which)
    interpreter = comspec or os.environ.get("COMSPEC", "cmd.exe")
    return [interpreter, "/c", resolved, *rest]


def _powershell_file(
    ps1: str,
    rest: list[str],
    powershell: str | None,
    which: Callable[[str], str | None],
) -> list[str]:
    """Build a non-interactive ``powershell -File`` launch for a ``.ps1`` shim (PowerShell 7 then 5)."""
    shell = powershell or which("pwsh") or which("powershell") or "powershell"
    return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, *rest]


def merged_env(overlay: dict[str, str] | None) -> dict[str, str] | None:
    """Overlay ``overlay`` on the inherited process environment.

    Returns ``None`` when there is nothing to overlay, so the child simply inherits the parent
    environment unchanged.
    """
    if not overlay:
        return None
    return {**os.environ, **overlay}
