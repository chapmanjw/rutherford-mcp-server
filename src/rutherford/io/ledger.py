# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The durable run ledger (F2): persist a run as a job on disk.

A run a caller opts to keep (Model A) is written under ``<root>/<run_id>/`` as ``state.json`` -- the
structured :class:`~rutherford.domain.models.RunRecord` as JSON -- plus Markdown ``artifacts/`` (the answer a
human reads, a diff for a write run, and for a panel one ``voices/voice-N.md`` per voice plus a debate
``transcript.md``). This writer is the only place that touches the jobs directory; the services hand it a
finished record and the answer text.

The record is JSON, NOT the TOON used on the MCP tool wire: ``state.json`` is internal -- only Rutherford's
own reader (job continuation / on-demand analysis) ever loads it back, and no LLM consumes it -- so it uses a
format that round-trips reliably rather than one optimized for an LLM's token budget. (TOON is reserved for
the tool payloads an MCP client actually reads; see :mod:`rutherford.io.serialize`.) Reading is
:func:`read_record`; the round trip is lossless, so a real argv with colon-bearing elements
(``gemma3:12b``, a Windows path) is read back exactly.

Persistence is best-effort by contract: a write failure must never fail the delegation that produced the
answer, so the caller wraps :meth:`RunLedger.write` and degrades to an unpersisted result. The writer
keeps no business logic -- it serializes and writes, nothing more.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.models import RunRecord
from .serialize import to_plain

#: The filename of the structured record under a run's directory.
RECORD_FILENAME = "state.json"


def record_to_json(record: RunRecord) -> str:
    """Serialize a :class:`RunRecord` to the on-disk JSON text (pretty, UTF-8, ``None`` fields dropped)."""
    return json.dumps(to_plain(record), indent=2, ensure_ascii=False)


def read_record(run_dir: Path) -> RunRecord:
    """Load and validate the :class:`RunRecord` from ``<run_dir>/state.json`` (the reader side of the ledger).

    The inverse of :meth:`RunLedger.write`'s record. Raises ``OSError`` if the file is missing/unreadable and
    a pydantic ``ValidationError`` / ``json.JSONDecodeError`` if the content is malformed -- the caller (job
    continuation) decides how to surface a corrupt or absent record.
    """
    text = (run_dir / RECORD_FILENAME).read_text(encoding="utf-8")
    return RunRecord.model_validate(json.loads(text))


class RunLedger:
    """Writes :class:`RunRecord` jobs under a root directory as ``state.json`` + Markdown artifacts."""

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

        ``state.json`` holds the structured record; ``artifacts/answer.md`` holds the answer the run
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
        (run_dir / RECORD_FILENAME).write_text(record_to_json(record), encoding="utf-8")
        (artifacts / "answer.md").write_text(answer if answer.strip() else "(no answer)", encoding="utf-8")
        if diff is not None and diff.strip():
            (artifacts / "diff.md").write_text(f"```diff\n{diff}\n```\n", encoding="utf-8")
        for name, content in (extra_artifacts or {}).items():
            path = artifacts / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content if content.strip() else "(empty)", encoding="utf-8")
        return run_dir
