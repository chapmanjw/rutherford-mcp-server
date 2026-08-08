# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests that the architecture described in AGENTS.md is the architecture that exists.

``AGENTS.md`` and ``CONTRIBUTING.md`` both state the layering rule -- dependencies point inward toward the
domain, the FastMCP layer stays thin, services never reach for a concrete agent -- and until this module
existed nothing checked any of it. A stated convention with no enforcement drifts silently: the violation
compiles, the tests pass, and it is only noticed when someone reads that file for another reason.

That gap matters more here than in most projects, because those instructions are addressed to coding
agents. An agent that misses the paragraph, or reasons its way past it, produces an outward import that
nobody catches. This test is the difference between documenting the architecture and having one.

The rule already holds. Nothing needed moving to make this pass; it locks a door that is already shut.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "rutherford"

#: Layers, outermost first. A package may import its own layer or anything below it, never above.
#: The numbers are the whole contract: `tools` may reach for `services`, `services` may reach for `acp`,
#: and nothing at the bottom may reach back up.
LAYERS: dict[str, int] = {
    "server": 4,
    "tools": 4,
    "context": 4,
    "services": 3,
    "acp": 2,
    "config": 1,
    "domain": 1,
    "io": 1,
    "runtime": 1,
}

#: Only the outermost layer may know the MCP framework exists. This is what keeps the core testable with
#: the fake ACP agent and no server at all.
FRAMEWORK_ROOTS = frozenset({"fastmcp", "mcp"})

#: The one accepted outward edge, recorded rather than silently permitted.
#:
#: ``config/loader.py`` reads the built-in roster to resolve agent defaults, and ``acp`` reads config to
#: build descriptors, so a module-level import either way is a cycle. The import is therefore made INSIDE
#: the function that needs it, which keeps the module-level graph acyclic and correctly directed. Listing it
#: here means the exception has to be justified deliberately rather than accumulating siblings quietly.
KNOWN_DEFERRED_IMPORTS: frozenset[tuple[str, str]] = frozenset({("config", "acp")})


def _module_level_imports(source: str) -> set[str]:
    """Root package names imported at MODULE level, ignoring imports made inside a function or class.

    Module level is what determines the import graph and therefore what can deadlock on a cycle. An import
    deferred into a function body is a different, weaker kind of dependency, and this project uses exactly
    one of them on purpose -- see :data:`KNOWN_DEFERRED_IMPORTS`.
    """
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in tree.body:  # top level only, deliberately not ast.walk
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _relative_target(node: ast.ImportFrom, parts: tuple[str, ...]) -> str | None:
    """Resolve a relative ``from ..x import y`` to the package it lands in, or ``None`` if it stays inside."""
    if not node.level or not node.module:
        return None
    base = list(parts[:-1])
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    resolved = [*base, *node.module.split(".")]
    return resolved[0] if resolved else None


def _package_of(path: Path) -> str:
    rel = path.relative_to(PACKAGE)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _edges() -> dict[str, set[str]]:
    """Every module-level edge between the package's own layers."""
    edges: dict[str, set[str]] = collections.defaultdict(set)
    for file in PACKAGE.rglob("*.py"):
        package = _package_of(file)
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                target = _relative_target(node, file.relative_to(PACKAGE).parts)
                if target and target != package:
                    edges[package].add(target)
        for root in _module_level_imports(source):
            if root in LAYERS and root != package:
                edges[package].add(root)
    return dict(edges)


def test_dependencies_point_inward() -> None:
    """No package imports one at a higher layer than itself.

    This is the rule AGENTS.md states in prose. Asserting it means a change that inverts a dependency fails
    the build with the specific edge named, rather than passing review because the diff looked local.
    """
    violations = [
        f"{source} (layer {LAYERS[source]}) imports {target} (layer {LAYERS[target]})"
        for source, targets in _edges().items()
        if source in LAYERS
        for target in targets
        if target in LAYERS and LAYERS[target] > LAYERS[source]
    ]
    assert not violations, (
        "dependencies must point inward toward the domain; these point outward:\n  "
        + "\n  ".join(violations)
        + "\nIf the dependency is genuinely needed, the usual fix is to move the shared type down a layer, "
        "not to invert the edge."
    )


def test_only_the_outer_layer_knows_the_mcp_framework() -> None:
    """FastMCP stays in the tool/server layer, which is what keeps the core testable without a server."""
    violations = [
        f"{source} imports {target}"
        for source, targets in _edges().items()
        for target in targets & FRAMEWORK_ROOTS
        if LAYERS.get(source, 0) < 4
    ]
    assert not violations, "only the tool/server layer may import the MCP framework:\n  " + "\n  ".join(violations)


def test_the_known_deferred_import_is_still_the_only_one() -> None:
    """The single accepted outward edge must stay single, and stay deferred.

    Written as an equality rather than a subset check on purpose. A new function-level import that dodges
    the layering rule would otherwise slip in beside this one and inherit its justification without ever
    being argued for.
    """
    found: set[tuple[str, str]] = set()
    for file in PACKAGE.rglob("*.py"):
        package = _package_of(file)
        if package not in LAYERS:
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"))
        module_level = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or id(node) in module_level:
                continue
            target = _relative_target(node, file.relative_to(PACKAGE).parts)
            if target in LAYERS and LAYERS[target] > LAYERS[package]:
                found.add((package, target))

    assert found == KNOWN_DEFERRED_IMPORTS, (
        f"the set of deferred outward imports changed.\n  expected: {sorted(KNOWN_DEFERRED_IMPORTS)}\n"
        f"  found:    {sorted(found)}\n"
        "A deferred import still creates a runtime dependency; it only hides the cycle from the module "
        "graph. Adding one needs the same argument the existing one had."
    )


@pytest.mark.parametrize("package", sorted(LAYERS))
def test_every_declared_layer_exists(package: str) -> None:
    """Guard against this test quietly checking nothing after a rename."""
    assert (PACKAGE / package).is_dir() or (PACKAGE / f"{package}.py").is_file(), (
        f"LAYERS names {package!r}, which no longer exists; the layering map is stale"
    )
