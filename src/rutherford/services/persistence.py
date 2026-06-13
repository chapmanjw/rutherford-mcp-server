# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Panel-level run persistence (F2): write the parent record that links a panel's child voices.

A persisted consensus/debate is one parent job directory whose ``state.toon`` is a :class:`RunRecord`
of ``kind`` ``consensus`` / ``debate`` carrying ``child_run_ids`` -- the run ids of the per-voice child
records (each a normal leaf delegate record persisted under its own dir with ``parent_run_id`` set). The
parent's ``artifacts/answer.md`` holds the synthesis, a consensus adds one ``artifacts/voices/voice-N.md``
per voice (and ``voices/skipped.md`` for an auto-panel's skipped adapters), and a debate adds
``artifacts/transcript.md``. So a reader can open the parent and walk to every voice. This module owns only
the *parent* write; the child voice records are written by the delegation service as each voice runs.

The parent is built to be self-auditable and replay-complete (decision 1-D): its ``status`` is *derived*
from the voices (succeeded when any voice answered, failed when none did), and it rolls up the panel-level
fields -- wall-clock duration, the request's safety mode / files / role, the union of the voices' changed
files, and the summed cost -- rather than leaving the parent a thin link record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import JobStatus, SafetyMode
from ..domain.models import Cost, RunRecord
from ..io.ledger import RunLedger
from ..runtime.logging import log_event


@dataclass(frozen=True)
class PanelVoice:
    """One voice's outcome as the panel parent records it: enough to derive status, audit, and roll up.

    ``run_id`` is the voice's child-record id (the basename of its run dir), or ``None`` when that voice
    did not persist. ``text`` and ``error`` feed the per-voice ``voices/voice-N.md`` artifact; ``cost`` and
    ``changed_files`` feed the parent's rollup (decision 1-D).
    """

    label: str
    ok: bool
    run_id: str | None = None
    text: str = ""
    error: str | None = None
    cost: Cost | None = None
    changed_files: tuple[str, ...] = field(default_factory=tuple)


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
    safety_mode: SafetyMode = SafetyMode.READ_ONLY,
    files: list[str] | None = None,
    role: str | None = None,
    extra_artifacts: dict[str, str] | None = None,
) -> str | None:
    """Write a panel's parent :class:`RunRecord` linking its child voice records; return its run_dir.

    Best-effort: a filesystem failure returns ``None`` rather than failing the panel that already
    produced an answer. ``clis`` are the distinct voice CLIs (a readable panel label); ``voices`` are the
    per-voice outcomes in panel order -- their ``run_id``s become the parent's ``child_run_ids``, their
    ``ok`` flags decide the parent ``status`` (succeeded when any voice answered, else failed), and their
    ``cost`` / ``changed_files`` roll up onto the parent. ``safety_mode`` / ``files`` / ``role`` are the
    panel request's, captured so the parent record is replay-complete (decision 1-D). ``extra_artifacts``
    carries the parent's audit artifacts -- a consensus passes its ``voices/voice-N.md`` set (see
    :func:`render_panel_voice_files`), a debate passes its ``transcript.md``.
    """
    status = JobStatus.SUCCEEDED if any(voice.ok for voice in voices) else JobStatus.FAILED
    record = RunRecord(
        run_id=run_id,
        kind=kind,
        status=status,
        created_at=created_at,
        finished_at=finished_at,
        duration_s=max(0.0, finished_at - created_at),
        child_run_ids=[voice.run_id for voice in voices if voice.run_id],
        cli=",".join(clis) if clis else kind,
        safety_mode=safety_mode,
        role=role,
        files=list(files or []),
        prompt=prompt,
        changed_files=_union_changed_files(voices),
        cost=_rollup_cost(voices),
    )
    try:
        run_dir = ledger.write(record, answer=answer or "(no synthesis)", extra_artifacts=extra_artifacts)
    except Exception as exc:  # best-effort: never fail a panel that already produced an answer over a bad write
        log_event("panel_persist_failed", run_id=run_id, kind=kind, error_type=type(exc).__name__, error=str(exc))
        return None
    return str(run_dir)


def render_panel_voice_files(voices: list[PanelVoice], skipped: list[tuple[str, str]] | None = None) -> dict[str, str]:
    """Render one ``voices/voice-N.md`` per voice (+ ``voices/skipped.md``) -- the locked F2 layout.

    Each voice's answer (or error) is written as its own Markdown file under ``artifacts/voices/`` so the
    parent is self-auditable without every child record still on disk, and so a reader who follows the
    locked ``artifacts/voices/voice-N.md`` path finds it. ``skipped`` (``(cli, reason)`` pairs from an
    auto-expanded panel's not-installed / unauthenticated / over-cap adapters) is written to
    ``voices/skipped.md`` so even an all-skipped panel -- which has no children to walk to -- explains
    itself from the parent alone.
    """
    artifacts: dict[str, str] = {}
    for index, voice in enumerate(voices, start=1):
        status = "" if voice.ok else " (failed)"
        body = voice.text.strip() if voice.ok and voice.text.strip() else (voice.error or "(no answer)")
        ref = f"\n\n_run: {voice.run_id}_" if voice.run_id else ""
        artifacts[f"voices/voice-{index}.md"] = f"# {voice.label}{status}\n\n{body}{ref}\n"
    if skipped:
        lines = ["# Skipped adapters\n\n"]
        lines.extend(f"- {cli}: {reason}\n" for cli, reason in skipped)
        artifacts["voices/skipped.md"] = "".join(lines)
    return artifacts


def _rollup_cost(voices: list[PanelVoice]) -> Cost | None:
    """Sum the voices' reported costs into one panel cost, or ``None`` when no voice reported any.

    Each field is summed only over the voices that reported it (a missing field does not zero the total);
    a field no voice reported stays ``None``, so an all-unpriced panel rolls up to ``None`` rather than a
    misleading zero.
    """
    costs = [voice.cost for voice in voices if voice.cost is not None]
    usd = [cost.usd for cost in costs if cost.usd is not None]
    input_tokens = [cost.input_tokens for cost in costs if cost.input_tokens is not None]
    output_tokens = [cost.output_tokens for cost in costs if cost.output_tokens is not None]
    total_tokens = [cost.total_tokens for cost in costs if cost.total_tokens is not None]
    if not (usd or input_tokens or output_tokens or total_tokens):
        return None
    return Cost(
        usd=sum(usd) if usd else None,
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
        total_tokens=sum(total_tokens) if total_tokens else None,
    )


def _union_changed_files(voices: list[PanelVoice]) -> list[str]:
    """The de-duplicated union of every voice's changed files, in first-seen order (decision 1-D)."""
    seen: dict[str, None] = {}
    for voice in voices:
        for name in voice.changed_files:
            seen.setdefault(name, None)
    return list(seen)
