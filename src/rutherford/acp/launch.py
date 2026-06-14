# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Cross-platform launch resolution for an agent's ACP-server command.

``asyncio.create_subprocess_exec`` runs a real executable directly, but a Windows npm ``.cmd`` / ``.bat``
shim is not directly executable, and launching one through ``cmd /c`` does not pass stdin reliably -- which
breaks ACP's JSON-RPC-over-stdin transport. This resolves ``command[0]`` to its real path on PATH and, on
Windows, launches a shim through its sibling ``.ps1`` via PowerShell (the reliable stdin path), falling back
to ``cmd /c`` when no ``.ps1`` exists. A ``.exe`` is run directly. An unresolved command is returned
unchanged so the spawn fails naturally as ``ACP_SPAWN_FAILED`` (reported as "not installed").
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_POWERSHELL = ("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File")


def prepare_argv(argv: tuple[str, ...]) -> list[str]:
    """Resolve ``argv`` to a launchable command list for the current platform."""
    if not argv:
        return []
    resolved = shutil.which(argv[0])
    rest = list(argv[1:])
    if resolved is None:
        return list(argv)
    if os.name == "nt":
        path = Path(resolved)
        suffix = path.suffix.lower()
        if suffix in (".cmd", ".bat"):
            sibling = path.with_suffix(".ps1")
            if sibling.exists():
                return [*_POWERSHELL, str(sibling), *rest]
            return ["cmd.exe", "/c", str(path), *rest]
        if suffix == ".ps1":
            return [*_POWERSHELL, str(path), *rest]
    return [resolved, *rest]
