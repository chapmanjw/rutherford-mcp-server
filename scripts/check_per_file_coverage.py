# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Per-file coverage floor for the whole source tree.

The aggregate ``--cov-fail-under`` floor can hide a near-zero-coverage module behind a well-covered
core: one new helper or error path at 0% passes while the package average stays above 90%. This check
reads the ``.coverage`` data the unit run just produced and fails when any source file falls below the
per-file floor. Run after ``pytest`` (the ``just check`` recipe and CI order it that way).

It checks every file under ``src/rutherford`` -- not a hand-listed set of subtrees, which silently
rots when the layout changes (an earlier version named a ``src/rutherford/adapters`` tree that the v3
ACP rewrite removed, so the floor quietly stopped guarding the high-risk subprocess/sandbox code that
moved to ``acp/``). To make a recurrence loud rather than silent, each named tree must match at least
one file; a tree that matches none fails the check.

A tiny ``EXCLUDE`` set carves out files that are intentionally SINGLE-PLATFORM: their code (and the
tests that exercise it) run on only one OS, so they read as near-zero coverage on the others. Enforcing
a per-file floor on them would make CI red on every other platform for code that is correct and covered
where it matters. The aggregate floor and the owning platform's own CI cell still cover them; the
exclusion is explicit and documented rather than a silent whole-tree gap.
"""

from __future__ import annotations

import json
import subprocess
import sys

#: The minimum per-file line coverage. Deliberately lower than the 90% aggregate floor: it exists to
#: catch a NEW dead zone, not to ratchet every file.
FLOOR = 80.0

#: The source trees checked per-file. The whole package, so no module is ever silently omitted; each
#: must match at least one file (a tree that matches none is a layout-rot bug, not a pass).
TREES = ("src/rutherford",)

#: Single-platform files excluded from the per-file floor (see the module docstring). Keep this TINY and
#: justify every entry -- it is the one sanctioned way to silence the floor, so it must not become a dumping
#: ground for merely-undertested files.
EXCLUDE = (
    "src/rutherford/acp/launch.py",  # Windows npm-shim resolution; its tests are skipif(os.name != "nt"),
    # so it is ~15% on Linux/macOS and ~94% on Windows. The aggregate floor + the Windows cell cover it.
)


def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-o", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(
            "coverage data unavailable (run the unit suite first): " + completed.stderr.strip(),
            file=sys.stderr,
        )
        return 1
    data = json.loads(completed.stdout)
    failures: list[tuple[str, float]] = []
    per_tree: dict[str, int] = dict.fromkeys(TREES, 0)
    for path, entry in sorted(data["files"].items()):
        norm = path.replace("\\", "/")
        if norm in EXCLUDE:
            continue
        tree = next((t for t in TREES if norm.startswith(t)), None)
        if tree is None:
            continue
        per_tree[tree] += 1
        percent = float(entry["summary"]["percent_covered"])
        if percent < FLOOR:
            failures.append((norm, percent))
    # A named tree that matched ZERO files is a stale path (the layout moved out from under it), not a
    # pass -- fail loudly so the floor can never again silently stop guarding a renamed/removed tree.
    empty = [tree for tree, count in per_tree.items() if count == 0]
    if empty:
        print(f"per-file coverage trees matched no files (stale path?): {', '.join(empty)}", file=sys.stderr)
        return 1
    if failures:
        print(f"per-file coverage below the {FLOOR:.0f}% floor:", file=sys.stderr)
        for norm, percent in failures:
            print(f"  {norm}: {percent:.1f}%", file=sys.stderr)
        return 1
    checked = sum(per_tree.values())
    print(f"per-file coverage floor ({FLOOR:.0f}%) holds across {checked} files in {len(TREES)} tree(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
