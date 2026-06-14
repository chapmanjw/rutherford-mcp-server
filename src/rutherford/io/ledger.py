# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The durable run ledger (F2): persist a run as a job on disk.

A run a caller opts to keep (Model A) is written under ``<root>/<run_id>/`` as ``state.toon`` -- the
structured :class:`~rutherford.domain.models.RunRecord`, TOON-encoded through the one serialization
seam (:mod:`rutherford.io.serialize`) -- plus Markdown ``artifacts/`` (the answer a human reads, a diff
for a write run, and for a panel one ``voices/voice-N.md`` per voice plus a debate ``transcript.md``).
This writer is the only place that touches the jobs directory; the services hand it a finished record and
the answer text.

Persistence is best-effort by contract: a write failure must never fail the delegation that produced the
answer, so the caller wraps :meth:`RunLedger.write` and degrades to an unpersisted result. The writer
keeps no business logic -- it serializes and writes, nothing more.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.models import RunRecord
from .serialize import encode


class RunLedger:
    """Writes :class:`RunRecord` jobs under a root directory as ``state.toon`` + Markdown artifacts."""

    def __init__(self, root: Path) -> None:
        #: The jobs root, e.g. ``<workspace>/.rutherford/jobs``. Created lazily on the first write.
        self._root = root

    @property
    def root(self) -> Path:
        """The jobs root directory this ledger writes under."""
        return self._root

    def write(
        self,
        record: RunRecord,
        *,
        answer: str,
        diff: str | None = None,
        extra_artifacts: dict[str, str] | None = None,
    ) -> Path:
        """Persist ``record`` and its artifacts under ``<root>/<run_id>/``; return the run directory.

        ``state.toon`` holds the structured record; ``artifacts/answer.md`` holds the answer the run
        produced (or a placeholder when empty, so the file always exists); ``artifacts/diff.md`` holds
        ``diff`` as a fenced block when a write run captured one; ``extra_artifacts`` maps additional
        filenames to Markdown content under ``artifacts/`` (e.g. a panel's ``voices/voice-1.md`` or a
        ``transcript.md``). A filename may contain a subdirectory (``voices/voice-1.md``); it is created
        as needed. Directories are created as needed.

        Raises ``OSError`` on a filesystem failure -- the caller treats persistence as best-effort and must
        not let that failure escape and fail the delegation that already produced an answer.
        """
        run_dir = self._root / record.run_id
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.toon").write_text(encode(record), encoding="utf-8")
        (artifacts / "answer.md").write_text(answer if answer.strip() else "(no answer)", encoding="utf-8")
        if diff is not None and diff.strip():
            (artifacts / "diff.md").write_text(f"```diff\n{diff}\n```\n", encoding="utf-8")
        for name, content in (extra_artifacts or {}).items():
            path = artifacts / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if content.strip() else "(empty)", encoding="utf-8")
        return run_dir
