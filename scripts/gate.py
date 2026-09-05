# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Run the full gate and write a machine-readable verdict a coding agent can act on.

``just check`` used to be a list of recipe dependencies whose only machine-readable output was a process
exit code: enough to answer "did it pass", not enough to answer "which stage failed" without reading prose
written for a human. This runs the same stages, streams the same output, and additionally records what
happened to each one.

Two design points are load-bearing rather than incidental.

**This IS the gate, not a description of it.** ``just check`` calls this script, so the stage list here is
the only local definition and cannot drift from the thing that actually runs. A report that says ``pass``
while the real gate ran a different set of stages would be worse than no report, because it manufactures
confidence exactly where someone would trust it. ``tests/test_gate.py`` extends that guarantee to CI by
asserting these commands match the workflow's, since the workflow keeps its own separately named steps for
the failure-diagnosis UI GitHub gives it.

**The report records whether the tree was clean.** Keying it to ``HEAD`` alone would let a run with
uncommitted edits report that commit as green, when what passed was the commit plus whatever was in the
worktree. ``dirty`` is how a reader tells those apart. This does not enforce a commit policy; it only
declines to be misleading about what was tested.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / ".tmp" / "gate-report.toon"

#: The report's shape. Bump when a consumer would need to care about a change.
SCHEMA_VERSION = 1

#: The gate, in order. Order matters and is not cosmetic: ``coverage-per-file`` reads the coverage data
#: ``test`` produces, so it cannot run first, and a failing stage stops the run rather than burning minutes
#: on stages whose inputs are already known bad. Commands are spelled exactly as the CI workflow spells them
#: so the two can be compared mechanically.
STAGES: tuple[tuple[str, str], ...] = (
    ("lint", "uv run ruff check ."),
    ("format-check", "uv run ruff format --check ."),
    ("license-check", "uv run python scripts/check_license_headers.py"),
    ("typecheck", "uv run mypy"),
    ("test", "uv run pytest"),
    ("coverage-per-file", "uv run python scripts/check_per_file_coverage.py"),
    ("smoke", "uv run python -m rutherford --smoke"),
    ("server-boot", "uv run python scripts/check_server_boot.py"),
    ("build", "uv build"),
)


def _git(*args: str) -> str | None:
    """Return trimmed ``git`` output, or ``None`` when git is unavailable or the call fails.

    Never raises: the gate's job is to report on the stages, and it should still do that in a directory
    where git cannot answer.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed `git` argv0 and literal subcommands, no shell
            ["git", *args],  # noqa: S607 - `git` from PATH, as everywhere else in this project
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _encode(payload: dict[str, Any]) -> tuple[str, str]:
    """Encode the report as TOON, falling back to JSON if the package cannot be imported.

    The project serializes agent-facing payloads as TOON and this is one, so it goes through the same seam
    rather than growing a second encoder. That seam lives inside the package this script exists to test,
    which is a coupling worth naming: the one run where the import fails is a run where something is badly
    wrong, and that is precisely when swallowing the report would be least helpful. So a failed import
    downgrades the format and says so in the payload, rather than losing the verdict.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from rutherford.io.serialize import encode
    except Exception:  # any import failure at all -- broken package, bad path -- must still yield a report
        payload["format_fallback"] = "json (the TOON seam could not be imported)"
        return json.dumps(payload, indent=2), "json"
    return encode(payload), "toon"


def main() -> int:
    """Run every stage in order, write the report, and exit non-zero if any stage failed."""
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    results: list[dict[str, Any]] = []
    failed: str | None = None

    for name, command in STAGES:
        print(f"\n=== {name}: {command} ===", flush=True)
        started = time.monotonic()
        # * Stream rather than capture: a human watching this still needs to see why a stage failed, and the
        # report is an addition to that output, never a replacement for it.
        completed = subprocess.run(command.split(), cwd=REPO_ROOT, check=False)  # noqa: S603 - literal argv from STAGES
        seconds = round(time.monotonic() - started, 2)
        ok = completed.returncode == 0
        results.append({"name": name, "ok": ok, "seconds": seconds, "exit_code": completed.returncode})
        if not ok:
            failed = name
            # * Stop here, exactly as the recipe chain did. Later stages consume earlier output.
            break

    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "head": head,
        # * None when git could not answer, which is different from "clean" and must not be conflated.
        "dirty": None if status is None else bool(status),
        "verdict": "pass" if failed is None else "fail",
        "failed_stage": failed,
        "stages_run": len(results),
        "stages_total": len(STAGES),
        "stages": results,
    }
    text, encoding = _encode(payload)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # * Write-then-replace: an interrupted run must not leave a half-written file that parses as a verdict.
    temporary = REPORT_PATH.with_suffix(REPORT_PATH.suffix + ".partial")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(REPORT_PATH)

    verdict = payload["verdict"]
    print(f"\n=== gate: {verdict} ({len(results)}/{len(STAGES)} stages run) ===")
    if failed is not None:
        print(f"=== failed at: {failed} ===")
    print(f"=== report ({encoding}): {REPORT_PATH.relative_to(REPO_ROOT)} ===")
    return 0 if failed is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
