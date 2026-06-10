# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Per-file coverage floor for the high-risk trees (adapters/, services/, runtime/).

The aggregate ``--cov-fail-under`` floor can hide a near-zero-coverage module behind a
well-covered core: a new adapter or error path at 0% passes while the package average stays above
90%. This check reads the ``.coverage`` data the unit run just produced and fails when any file
under the named trees falls below the per-file floor. Run after ``pytest`` (the ``just check``
recipe and CI order it that way).
"""

from __future__ import annotations

import json
import subprocess
import sys

#: The minimum per-file line coverage for the trees below. Deliberately lower than the 90%
#: aggregate floor: it exists to catch a NEW dead zone, not to ratchet every file.
FLOOR = 80.0

#: The trees where an undertested file is an operational risk (subprocess lifecycle, safety
#: mapping, output parsing, orchestration) rather than presentation.
TREES = ("src/rutherford/adapters", "src/rutherford/services", "src/rutherford/runtime")


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
    checked = 0
    for path, entry in sorted(data["files"].items()):
        norm = path.replace("\\", "/")
        if not norm.startswith(TREES):
            continue
        checked += 1
        percent = float(entry["summary"]["percent_covered"])
        if percent < FLOOR:
            failures.append((norm, percent))
    if not checked:
        print("no files matched the per-file coverage trees; is the coverage data stale?", file=sys.stderr)
        return 1
    if failures:
        print(f"per-file coverage below the {FLOOR:.0f}% floor:", file=sys.stderr)
        for norm, percent in failures:
            print(f"  {norm}: {percent:.1f}%", file=sys.stderr)
        return 1
    print(f"per-file coverage floor ({FLOOR:.0f}%) holds across {checked} files in {len(TREES)} trees.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
