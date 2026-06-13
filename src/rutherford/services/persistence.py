# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Panel-level run persistence (F2): write the parent record that links a panel's child voices.

A persisted consensus/debate is one parent job directory whose ``state.toon`` is a :class:`RunRecord`
of ``kind`` ``consensus`` / ``debate`` carrying ``child_run_ids`` -- the run ids of the per-voice child
records (each a normal leaf delegate record persisted under its own dir with ``parent_run_id`` set). The
parent's ``artifacts/answer.md`` holds the synthesis, and a debate adds ``artifacts/transcript.md``. So a
reader can open the parent and walk to every voice. This module owns only the *parent* write; the child
voice records are written by the delegation service as each voice runs.

The parent is built to be self-auditable: its ``status`` is *derived* from the voices (succeeded when any
voice answered, failed when none did), not assumed, and when there is no synthesis a ``voices.md`` artifact
inlines each voice's answer or error so the panel can be read without every child record still on disk.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import JobStatus
from ..domain.models import RunRecord
from ..io.ledger import RunLedger
from ..runtime.logging import log_event


@dataclass(frozen=True)
class PanelVoice:
    """One voice's outcome as the panel parent records it: enough to derive status and audit the panel.

    ``run_id`` is the voice's child-record id (the basename of its run dir), or ``None`` when that voice
    did not persist. ``text`` and ``error`` feed the ``voices.md`` artifact so the parent is readable
    without walking to every child.
    """

    label: str
    ok: bool
    run_id: str | None = None
    text: str = ""
    error: str | None = None


def write_panel_record(
    ledger: RunLedger,
    *,
    run_id: str,
    kind: str,
    prompt: str,
    clis: list[str],
    voices: list[PanelVoice],
    answer: str,
    created_at: float,
    finished_at: float,
    extra_artifacts: dict[str, str] | None = None,
) -> str | None:
    """Write a panel's parent :class:`RunRecord` linking its child voice records; return its run_dir.

    Best-effort: a filesystem failure returns ``None`` rather than failing the panel that already
    produced an answer. ``clis`` are the distinct voice CLIs (a readable panel label); ``voices`` are the
    per-voice outcomes in panel order -- their ``run_id``s become the parent's ``child_run_ids`` and their
    ``ok`` flags decide the parent ``status`` (succeeded when any voice answered, else failed, rather than
    assumed). ``extra_artifacts`` carries the parent's audit artifact -- a consensus without synthesis
    passes a ``voices.md`` (see :func:`render_panel_voices`), a debate passes its ``transcript.md`` -- so
    the parent is readable without every child record still on disk.
    """
    status = JobStatus.SUCCEEDED if any(voice.ok for voice in voices) else JobStatus.FAILED
    record = RunRecord(
        run_id=run_id,
        kind=kind,
        status=status,
        created_at=created_at,
        finished_at=finished_at,
        child_run_ids=[voice.run_id for voice in voices if voice.run_id],
        cli=",".join(clis) if clis else kind,
        prompt=prompt,
    )
    try:
        run_dir = ledger.write(record, answer=answer or "(no synthesis)", extra_artifacts=extra_artifacts)
    except Exception as exc:  # best-effort: never fail a panel that already produced an answer over a bad write
        log_event("panel_persist_failed", run_id=run_id, kind=kind, error_type=type(exc).__name__, error=str(exc))
        return None
    return str(run_dir)


def render_panel_voices(voices: list[PanelVoice], skipped: list[tuple[str, str]] | None = None) -> str:
    """Render each voice's answer (or error) as a Markdown ``voices.md`` so the parent is self-auditable.

    Used when a panel has no free-text synthesis, so the parent's ``answer.md`` would otherwise be a bare
    placeholder and the voices would live only in child records. Each voice notes its child ``run_id`` for
    a reader who wants the full record. ``skipped`` (``(cli, reason)`` pairs, e.g. an auto-expanded panel's
    not-installed / unauthenticated / over-cap adapters) is inlined under a closing section so even an
    all-skipped panel -- which has no children to walk to -- still explains itself from the parent alone.
    """
    lines = ["# Panel voices\n"]
    for voice in voices:
        status = "" if voice.ok else " (failed)"
        body = voice.text.strip() if voice.ok and voice.text.strip() else (voice.error or "(no answer)")
        ref = f"\n\n_run: {voice.run_id}_" if voice.run_id else ""
        lines.append(f"\n## {voice.label}{status}\n\n{body}{ref}\n")
    if skipped:
        lines.append("\n## Skipped\n\n")
        lines.extend(f"- {cli}: {reason}\n" for cli, reason in skipped)
    return "".join(lines)
