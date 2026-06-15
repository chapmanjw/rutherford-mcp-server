# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Panel-level run persistence (F2): write the parent record that links a panel's child voices.

A persisted consensus/debate is one parent job directory whose ``state.json`` is a :class:`RunRecord`
of ``kind`` ``consensus`` / ``debate`` carrying ``child_run_ids`` -- the run ids of the per-voice child
records (each a normal leaf delegate record persisted under its own dir with ``parent_run_id`` set). The
parent's ``artifacts/answer.md`` holds the synthesis, a consensus adds one ``artifacts/voices/voice-N.md``
per voice (and ``voices/skipped.md`` for an auto-panel's skipped agents), and a debate adds
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
from ..domain.models import Cost, PanelInputs, RunRecord, RunRollup, StoredVerdict, Topology
from ..io.ledger import RunLedger
from ..runtime.logging import log_event


@dataclass(frozen=True)
class PanelVoice:
    """One voice's outcome as the panel parent records it: enough to derive status, audit, and roll up.

    ``run_id`` is the voice's child-record id (the basename of its run dir), or ``None`` when that voice
    did not persist. ``text`` and ``error`` feed the per-voice ``voices/voice-N.md`` artifact; ``cost`` and
    ``changed_files`` feed the parent's rollup (decision 1-D). ``partial`` is the text a voice streamed
    before a time-budget cut (F8a): a cut voice has no ``text``, but its partial is persisted into the voice
    artifact so the in-flight work is not lost. ``None`` for a voice that finished cleanly.
    """

    label: str
    ok: bool
    run_id: str | None = None
    text: str = ""
    error: str | None = None
    cost: Cost | None = None
    changed_files: tuple[str, ...] = field(default_factory=tuple)
    partial: str | None = None
    #: The voice's resume session handle, recorded so a budget-cut voice can be resumed later (F8a, 2-I).
    #: A completed voice keeps its own child record, so this matters most for a cut voice (which has none):
    #: its handle, recovered from the harvested partial, lands in the voice artifact. ``None`` when unknown.
    session_id: str | None = None


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
    cwd: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    panel: PanelInputs | None = None,
    verdicts: list[StoredVerdict] | None = None,
    stop_reason: str | None = None,
    rollup: RunRollup | None = None,
    topology: Topology | None = None,
    continued_from: str | None = None,
    extra_artifacts: dict[str, str] | None = None,
) -> str | None:
    """Write a panel's parent :class:`RunRecord` linking its child voice records; return its run_dir.

    Best-effort: a filesystem failure returns ``None`` rather than failing the panel that already
    produced an answer. ``clis`` are the distinct voice CLIs (a readable panel label); ``voices`` are the
    per-voice outcomes in panel order -- their ``run_id``s become the parent's ``child_run_ids``, their
    ``ok`` flags decide the parent ``status`` (succeeded when any voice answered, else failed), and their
    ``cost`` / ``changed_files`` roll up onto the parent. ``safety_mode`` / ``cwd`` / ``files`` / ``role`` /
    ``panel`` are the panel request's (``panel`` is the resolved orchestration config -- roster, strategy,
    rounds, ...), captured so the parent record is replay-complete (decision 1-D). ``verdicts`` is the
    per-voice verdict projection of a tally-strategy consensus (F3 cross-run), ``None`` otherwise. ``stop_reason`` /
    ``rollup`` record a time-budget harvest (F8a): ``"budget"`` and the per-run rollup when a budget cut the
    panel, both ``None`` on a normal completion. ``topology`` is the panel's observed process/agent fan-out
    (N1, item 3), ``None`` when not measured. ``extra_artifacts`` carries the parent's audit artifacts --
    a consensus passes its ``voices/voice-N.md`` set (see :func:`render_panel_voice_files`), a debate passes
    its ``transcript.md``.
    """
    status = JobStatus.SUCCEEDED if any(voice.ok for voice in voices) else JobStatus.FAILED
    record = RunRecord(
        run_id=run_id,
        kind=kind,
        status=status,
        # Keep ``ok`` consistent with the derived status: an all-failed panel must not record ok=true
        # (the RunRecord default), which would contradict its own status: failed (1-D outputs).
        ok=status is JobStatus.SUCCEEDED,
        created_at=created_at,
        finished_at=finished_at,
        duration_s=max(0.0, finished_at - created_at),
        child_run_ids=[voice.run_id for voice in voices if voice.run_id],
        # item 9: the panel this run continues, so a panel continuation chain is traceable from the parent.
        continued_from=continued_from,
        cli=",".join(clis) if clis else kind,
        safety_mode=safety_mode,
        cwd=cwd,
        role=role,
        files=list(files or []),
        panel=panel,
        # F3 cross-run (schema v2): the per-voice verdicts of a tally-strategy consensus, so a later
        # historical-agreement report can read which lineages agreed. ``None`` for all-voices / debate.
        verdicts=verdicts,
        prompt=prompt,
        changed_files=_union_changed_files(voices),
        cost=_rollup_cost(voices),
        stop_reason=stop_reason,
        rollup=rollup,
        topology=topology,  # N1 (item 3): the panel's observed process/agent fan-out
        # Mirror the rollup's effort onto the record's own effort fields so the parent agrees with its
        # rollup (and a reader scanning records by effort sees the panel, not just its leaves).
        requested_effort=rollup.effort_requested if rollup else None,
        effort_applied=rollup.effort_applied if rollup else None,
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
        if voice.ok and voice.text.strip():
            body = voice.text.strip()
        elif voice.partial and voice.partial.strip():
            # A voice cut at the time-budget deadline: no final answer, but the partial it streamed before
            # the cut is preserved here so the in-flight work lands in the job artifacts (F8a).
            reason = voice.error or "(cut at the time-budget deadline)"
            body = f"{reason}\n\n## Partial output (harvested at the cut)\n\n{voice.partial.strip()}"
        else:
            body = voice.error or "(no answer)"
        ref = f"\n\n_run: {voice.run_id}_" if voice.run_id else ""
        # Record the resume handle for a cut voice (which has no child record of its own), so a later
        # continuation can resume the session it was cut from (F8a, 2-I).
        ref += f"\n\n_session: {voice.session_id}_" if voice.session_id and not voice.run_id else ""
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
