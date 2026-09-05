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


def _on_disk_case(resolved: str) -> str:
    """Return ``resolved`` with its file name spelled the way the directory entry spells it.

    ``shutil.which`` never reads the dirent. It returns ``os.path.join(dir, thefile)`` where ``thefile`` is
    built from the CALLER's string, plus -- on Windows only -- each ``PATHEXT`` entry appended VERBATIM. So an
    uppercase ``PATHEXT`` hands back ``kiro-cli.EXE`` for a file whose dirent is ``kiro-cli.exe``. Windows opens
    either spelling, so the process starts; but a launcher that looks its own module name up in a table finds
    no entry for the uppercase form and exits before reading a byte of stdin, which an ACP client can only
    report as an instant "Connection closed". A managed bin directory whose every entry is the same dispatcher
    stub, distinguished only by the name it is invoked under, is exactly that shape.

    NOT guarded on ``os.name == "nt"``, and that is a correctness choice rather than a cosmetic one. The
    ``PATHEXT`` mechanism is Windows-only, but the DEFECT is not: macOS ships a case-insensitive APFS/HFS+ by
    default, where ``which("Kiro-CLI")`` likewise resolves a dirent named ``kiro-cli`` and returns the caller's
    spelling, and POSIX ``execve`` passes that spelling straight through as ``argv[0]``. On a case-SENSITIVE
    filesystem the exact-match branch below returns the input untouched, so this is self-neutralizing there --
    one ``scandir`` (0.35 ms against a 5,111-entry System32) against a process spawn that costs orders more.

    Case is normalized WITHOUT ``Path.resolve()`` / ``os.path.realpath`` on purpose: those also dereference
    links, and measured against a real ``mklink /J`` junction that rewrote ``...\\nodejs\\NODE.EXE`` to
    ``...\\node-v20.11.0\\node.exe`` -- pinning a version-managed shim to one concrete install so a later
    ``nvm use`` silently would not take effect. A leaf-level ``is_symlink`` guard does not rescue that, because
    nvm-style layouts put the junction on the PARENT directory.

    An ``exists()`` probe cannot shortcut this either: it is case-INSENSITIVE on exactly the platforms carrying
    the bug (measured ``True`` for ``KIRO-CLI.EXE`` against a dirent spelled ``Kiro-Cli.exe``), so it would
    always report the corrupted spelling as fine and make the whole fix a silent no-op.

    ``lower()`` rather than ``casefold()``: casefold is deliberately more aggressive than filesystem
    case-insensitivity (it folds ``ß`` to ``ss``), so it can equate two names NTFS considers DIFFERENT files --
    and rewriting to a different file is a worse failure than the one being fixed.
    """
    path = Path(resolved)
    name = path.name
    folded = name.lower()
    candidate: str | None = None
    ambiguous = False
    try:
        with os.scandir(path.parent) as entries:
            for entry in entries:
                if entry.name == name:
                    # An exact dirent exists: nothing to correct. Returned from INSIDE the loop so a
                    # case-variant enumerated earlier can never win over it -- scandir order is unspecified,
                    # and "first case-insensitive hit" would resolve the same input differently per machine.
                    return resolved
                if entry.name.lower() == folded:
                    if candidate is None:
                        candidate = entry.name
                    else:
                        # Two dirents differing only in case, on a case-SENSITIVE filesystem. Either could be
                        # the intended target, so keep scanning for an exact match but never guess between them.
                        ambiguous = True
    except OSError:
        # A PATH directory that vanished, denied enumeration, or raced. shutil.which already found the file by
        # name, so the honest fallback is the spelling we have -- never a crash in a previously working spawn.
        return resolved
    if candidate is None or ambiguous:
        return resolved
    return str(path.with_name(candidate))


def prepare_argv(argv: tuple[str, ...]) -> list[str]:  # noqa: C901 - per-platform launch cases, each distinct
    """Resolve ``argv`` to a launchable command list for the current platform."""
    if not argv:
        return []
    resolved = shutil.which(argv[0])
    rest = list(argv[1:])
    if resolved is None:
        return list(argv)
    # Normalize ONCE at the single resolution point, before any branching, so every return path below inherits
    # it: the direct ``.exe`` (where the diagnosed failure lands), the shell-wrapper fallbacks (which pass the
    # path to cmd.exe / PowerShell, so a ``.BAT`` dispatcher inspecting ``%0`` sees the real spelling), and the
    # sibling lookups, whose stems are derived from this name.
    resolved = _on_disk_case(resolved)
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


def _resolve_npm_shim(shim: Path) -> list[str] | None:  # noqa: C901 - Windows shim shapes, enumerated
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
            # A node path parsed OUT of the shim text already carries the real spelling (it is a literal in the
            # file). The PATH fallback does not: this is a SECOND ``shutil.which`` and reproduces the uppercase
            # -PATHEXT defect verbatim -- measured on this machine, ``which("node")`` returns ``node.EXE`` for a
            # dirent spelled ``node.exe``. Normalizing only the top-level resolution would leave the bug alive
            # at this nested boundary, so it is applied here too.
            fallback = shutil.which("node")
            node = next(
                (str(item) for item in candidates if item.name.lower() == "node.exe"),
                None if fallback is None else _on_disk_case(fallback),
            )
            if node is not None:
                return [node, str(scripts[0])]
    return None
