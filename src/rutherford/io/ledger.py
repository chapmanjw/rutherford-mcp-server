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
import shutil
from collections.abc import Iterator
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


def iter_records(root: Path) -> Iterator[tuple[Path, RunRecord]]:
    """Yield every readable kept ``(run_dir, RunRecord)`` under ``root``, newest-first by ``created_at``.

    The corpus reader (F3 cross-run / on-demand analysis): the only batch view over the jobs tree. Best-effort,
    like the writer -- a child dir whose ``state.json`` is missing, unreadable, malformed, or fails validation
    (a partial write, a foreign directory, an older incompatible record) is SKIPPED, never raised, so one bad
    record cannot break a sweep over the rest. A missing/empty root yields nothing. Reads every record to sort,
    so the cost is the corpus size -- bounded by what the caller chose to keep.
    """
    if not root.is_dir():
        return
    pairs: list[tuple[Path, RunRecord]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            record = read_record(child)
        except (
            OSError,
            ValueError,
        ):  # missing/unreadable (OSError) or malformed/invalid (JSON + pydantic ⊂ ValueError)
            continue
        pairs.append((child, record))
    pairs.sort(key=lambda pair: pair[1].created_at, reverse=True)
    yield from pairs


def read_answer(run_dir: Path) -> str:
    """The answer text a run produced, from ``<run_dir>/artifacts/answer.md`` (for continuation re-injection).

    Returns ``""`` when the artifact is missing or empty -- a continuation degrades to re-injecting only the
    original prompt rather than failing. Raises ``OSError`` only on an unreadable (not absent) file.
    """
    answer_path = run_dir / "artifacts" / "answer.md"
    if not answer_path.exists():
        return ""
    text = answer_path.read_text(encoding="utf-8").strip()
    return "" if text == "(no answer)" else text


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

    def remove(self, run_id: str) -> None:
        """Delete a run's directory (best-effort). The ledger owns the jobs tree, so deletion lives here too.

        Used to drop a throwaway record -- e.g. a continuation's failed-resume probe that was superseded by
        the re-injection fallback (item 9), so the continuation chain keeps only the real child. A ``run_id``
        is a single directory name; a missing directory is a silent no-op. A ``run_id`` that is not a plain
        single component (``""`` / ``.`` / ``..`` / a separator) is refused -- a delete must never be able to
        reach the root itself or its parent, even if a caller passed a malformed id.
        """
        if run_id in ("", ".", "..") or Path(run_id).name != run_id:
            return
        target = self._root / run_id
        if target.name == run_id and target.parent == self._root:
            shutil.rmtree(target, ignore_errors=True)
