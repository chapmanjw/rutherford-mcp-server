# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Verify the short license header on every Python source file.

The siblings enforce a license header in CI (the Java repo via Spotless). This is the
Python equivalent: every tracked ``.py`` file under ``src/``, ``tests/``, and ``scripts/``
must begin with the two-line SPDX header. Exits non-zero (listing offenders) when any file
is missing it, so the check can gate CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

HEADER_LINES = (
    "# SPDX-License-Identifier: MIT",
    "# Copyright (c) 2026 John Chapman",
)
ROOTS = ("src", "tests", "scripts")


def has_header(path: Path) -> bool:
    """Return whether ``path`` begins with the required two header lines."""
    with path.open(encoding="utf-8") as handle:
        first = handle.readline().rstrip("\n").rstrip("\r")
        second = handle.readline().rstrip("\n").rstrip("\r")
    return first == HEADER_LINES[0] and second == HEADER_LINES[1]


def main() -> int:
    """Scan the source roots and report any file missing the header."""
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[Path] = []
    for root in ROOTS:
        for path in sorted((repo_root / root).rglob("*.py")):
            if not has_header(path):
                offenders.append(path.relative_to(repo_root))
    if offenders:
        print("Missing license header (expected the two SPDX lines at the top):")
        for path in offenders:
            print(f"  - {path.as_posix()}")
        print("\nAdd these two lines to the top of each file:")
        for line in HEADER_LINES:
            print(f"  {line}")
        return 1
    print("License header present on all source files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
