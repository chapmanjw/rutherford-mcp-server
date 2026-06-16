# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Cross-platform launch resolution for an agent's ACP-server command.

``asyncio.create_subprocess_exec`` runs a real executable directly, but a Windows npm shim is not it. The
``.cmd`` shim launched via ``cmd /c`` and the ``.ps1`` shim launched via PowerShell both corrupt the raw
JSON-RPC stdin the ACP transport needs (cmd drops it; PowerShell's ``$input | & exe`` pipeline mangles it
into objects). So for an npm shim this resolves the shim to its REAL target -- the bundled ``.exe`` or the
``node <entry>.js`` it wraps -- and launches that directly with clean stdio. A non-npm shim falls back to
the ``.ps1`` sibling via PowerShell, then ``cmd /c``. A ``.exe`` is run directly. When ``shutil.which``
resolves to the EXTENSIONLESS npm bin (a Unix shell script Windows cannot exec), its ``.cmd`` / ``.ps1``
sibling is resolved instead. An unresolved command is returned unchanged so the spawn fails naturally as
``ACP_SPAWN_FAILED`` ("not installed").
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

_POWERSHELL = ("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File")
#: Quoted "..." tokens in a shim line, the form npm uses for the program and the script path.
_QUOTED = re.compile(r'"([^"]+)"')


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
        if suffix in (".cmd", ".bat", ".ps1"):
            target = _resolve_npm_shim(path)
            if target is not None:
                return [*target, *rest]
            if suffix == ".ps1":
                return [*_POWERSHELL, str(path), *rest]
            sibling = path.with_suffix(".ps1")
            if sibling.exists():
                return [*_POWERSHELL, str(sibling), *rest]
            return ["cmd.exe", "/c", str(path), *rest]
        if suffix == "":
            # ``shutil.which`` returned the EXTENSIONLESS npm bin -- a Unix shell script Windows cannot exec
            # (``CreateProcess`` -> WinError 193). It shadows the ``.cmd`` / ``.ps1`` siblings npm also installs
            # (PATHEXT resolution can land on the bare name first). Resolve via a sibling shim, which IS a real
            # npm shim wrapping ``[node, entry.js]`` / a bundled ``.exe`` -- so codex-acp / claude-agent-acp
            # launch with clean JSON-RPC stdio instead of failing as "not installed".
            for sibling_suffix in (".cmd", ".ps1"):
                sibling = path.with_name(path.name + sibling_suffix)
                if sibling.exists():
                    target = _resolve_npm_shim(sibling)
                    if target is not None:
                        return [*target, *rest]
                    if sibling_suffix == ".ps1":
                        return [*_POWERSHELL, str(sibling), *rest]
    return [resolved, *rest]


def _resolve_npm_shim(shim: Path) -> list[str] | None:
    """Parse an npm ``.cmd`` / ``.ps1`` shim for its real target: ``[exe]`` or ``[node, script.js]``.

    Returns ``None`` for a non-npm shim (no ``node_modules`` reference) so a JetBrains-style ``.bat`` etc.
    is left to the shell-wrapper fallback. A bundled native ``.exe`` (not ``node.exe``) is run directly;
    otherwise the wrapped script -- a ``.js`` or an extensionless ``#!/usr/bin/env node`` bin -- is run via
    ``node``.
    """
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "node_modules" not in text:
        return None
    base = str(shim.parent)
    for line in text.splitlines():
        if "%*" not in line and "$args" not in line:
            continue
        candidates: list[Path] = []
        for token in _QUOTED.findall(line):
            value = token.replace("%~dp0%", base).replace("%dp0%", base).replace("$basedir", base)
            if "%" in value or "$" in value:
                continue
            candidate = Path(value)
            if candidate.exists():
                candidates.append(candidate)
        exes = [item for item in candidates if item.suffix.lower() == ".exe" and item.name.lower() != "node.exe"]
        if exes:
            return [str(exes[0])]
        scripts = [item for item in candidates if item.name.lower() != "node.exe"]
        if scripts:
            node = next((str(item) for item in candidates if item.name.lower() == "node.exe"), shutil.which("node"))
            if node is not None:
                return [node, str(scripts[0])]
    return None
