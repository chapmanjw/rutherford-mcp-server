# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Every helper subprocess must detach its stdin from the MCP transport.

Rutherford runs as a stdio MCP server, so the parent's stdin is the live pipe the host holds a read on.
A child that inherits it can wedge on Windows in a way that survives ``kill()`` and clears only when the
server exits, which takes the host down with it.

Checked structurally rather than per call site: a refactor that moves or adds a spawn is exactly when
this gets dropped, and a test naming three functions would not notice.
"""

from __future__ import annotations

import ast
from pathlib import Path

import rutherford

#: Spawn calls per module. Keyed by module because the attribute name alone is ambiguous: ``asyncio.run``
#: is the event-loop runner and has nothing to do with subprocesses, so matching a bare "run" reports
#: every CLI entry point as a violation.
#:
#: The ACP adapter is spawned by the agent-client-protocol SDK, not from this tree, and its stdin IS the
#: transport -- it must stay attached. Any spawn written HERE is a helper and must not inherit.
_SPAWN_CALLS = {
    "subprocess": {"run", "Popen", "check_output", "check_call", "call"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}


def _spawn_sites() -> list[tuple[str, int, str, bool]]:
    """Every subprocess spawn under ``src/rutherford``, with whether it passes ``stdin``."""
    root = Path(rutherford.__file__).parent
    sites: list[tuple[str, int, str, bool]] = []
    for source in sorted(root.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            module = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
            if node.func.attr not in _SPAWN_CALLS.get(module, frozenset()):
                continue
            passes_stdin = any(kw.arg == "stdin" for kw in node.keywords)
            sites.append((source.name, node.lineno, node.func.attr, passes_stdin))
    return sites


def test_the_scan_finds_the_known_spawn_sites() -> None:
    """Guard the guard: a scan that silently matches nothing would pass the real test vacuously."""
    sites = _spawn_sites()
    assert len(sites) >= 4, f"expected the known helper spawns, found {sites}"


def test_every_spawn_detaches_stdin() -> None:
    inheriting = [f"{name}:{line} ({call})" for name, line, call, ok in _spawn_sites() if not ok]
    assert inheriting == [], "these spawns inherit the parent's stdin, which is the live MCP transport: " + ", ".join(
        inheriting
    )
