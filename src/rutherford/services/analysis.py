# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""On-demand analysis over the kept run corpus (F3 cross-run): the historical-agreement report.

The read-only, across-panel companion to the within-panel F3 vote-math. It scans the kept consensus records
(the panels a caller chose to persist), keys each voice's verdict on its model-family LINEAGE, and reports how
often two DISTINCT lineages reached the same verdict when they co-voted. It is OBSERVATIONAL by design and
never feeds a vote discount -- agreement is not correctness, so down-weighting agreeing lineages would punish
them for being right together (the unsound idea this module deliberately does NOT build). See
:class:`~rutherford.domain.models.HistoricalAgreementReport`.

Pure over its inputs: it reads the ledger corpus and computes; it never writes, never mutates a result, never
reshapes a live vote.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..domain.models import (
    HistoricalAgreementReport,
    LineageAgreement,
    Provenance,
    StoredVerdict,
)
from ..io.ledger import RunLedger, iter_records
from .strategies import lineage_key


class HistoricalAgreementService:
    """Computes the :class:`HistoricalAgreementReport` from the kept run corpus (F3 cross-run)."""

    def __init__(self, ledger: RunLedger) -> None:
        #: The durable run ledger; its ``root`` is the corpus this report scans (read-only).
        self._ledger = ledger

    def report(self) -> HistoricalAgreementReport:
        """Scan the kept consensus corpus and report cross-lineage agreement (read-only).

        Considers only ``kind == "consensus"`` records that carry per-voice ``verdicts`` (a tally-strategy
        panel; an all-voices consensus and every debate / leaf record have none). For each such panel, each
        lineage contributes ONE verdict iff its parseable voices were internally unanimous (a split lineage is
        excluded for that panel and counted in ``dropped_lineage_panels``); then every DISTINCT co-voting
        lineage pair tallies its co-vote count and its agreement count. Pairs are returned most-co-voted first
        (then by agreement rate, then lexically) so the strongest signal reads at the top.
        """
        consensus_records = [
            record for _, record in iter_records(self._ledger.root) if record.kind == "consensus" and record.verdicts
        ]
        pair_panels: Counter[tuple[str, str]] = Counter()
        pair_agreements: Counter[tuple[str, str]] = Counter()
        lineages: set[str] = set()
        panels_scanned = 0
        dropped_lineage_panels = 0
        for record in consensus_records:
            verdicts = record.verdicts or []
            lineage_verdicts, dropped = _panel_lineage_verdicts(verdicts)
            if dropped:
                dropped_lineage_panels += 1
            lineages.update(lineage_verdicts)
            keys = sorted(lineage_verdicts)
            if len(keys) < 2:
                continue  # a single lineage forms no pair
            panels_scanned += 1
            for i, key_a in enumerate(keys):
                for key_b in keys[i + 1 :]:  # distinct pairs only, a < b
                    pair = (key_a, key_b)
                    pair_panels[pair] += 1
                    if lineage_verdicts[key_a] == lineage_verdicts[key_b]:
                        pair_agreements[pair] += 1
        pairs = [
            LineageAgreement(
                a=pair[0],
                b=pair[1],
                panels=panels,
                agreements=pair_agreements[pair],
                agreement_rate=round(pair_agreements[pair] / panels, 4),
            )
            for pair, panels in pair_panels.items()
        ]
        # Strongest signal first: most co-votes, then highest agreement, then a stable lexical tiebreak.
        pairs.sort(key=lambda p: (-p.panels, -p.agreement_rate, p.a, p.b))
        return HistoricalAgreementReport(
            panels_scanned=panels_scanned,
            pairs=pairs,
            lineages=sorted(lineages),
            dropped_lineage_panels=dropped_lineage_panels,
            notes=_notes(len(consensus_records), panels_scanned),
        )


def _panel_lineage_verdicts(verdicts: list[StoredVerdict]) -> tuple[dict[str, str], bool]:
    """Reduce one panel's stored verdicts to ``{lineage_key: verdict}`` plus a "dropped a lineage" flag.

    A voice with no parseable verdict (failed / unparseable) is skipped silently -- it never had a verdict to
    agree on. A voice whose provenance does not resolve to a lineage is dropped AND flags ``dropped`` (the
    report surfaces it rather than silently shrinking). A lineage whose parseable voices DISAGREE among
    themselves is internally split: it contributes no panel verdict and flags ``dropped`` -- never coin-flipped
    to one side. The lineage key is recomputed here from raw provider+model, so an improved family table
    re-keys history.
    """
    by_lineage: dict[str, set[str]] = defaultdict(set)
    dropped = False
    for voice in verdicts:
        if not voice.ok or voice.verdict is None:
            continue  # no verdict to compare -- not a "dropped lineage", just an abstaining voice
        key = lineage_key(Provenance(provider=voice.provider, model=voice.model))
        if key is None:
            dropped = True  # provenance did not resolve to a lineage
            continue
        by_lineage[key].add(voice.verdict)
    out: dict[str, str] = {}
    for key, distinct in by_lineage.items():
        if len(distinct) == 1:
            out[key] = next(iter(distinct))
        else:
            dropped = True  # the lineage split internally -- no single verdict for it on this panel
    return out, dropped


def _notes(consensus_with_verdicts: int, panels_scanned: int) -> list[str]:
    """Advisory context lines so a thin or empty report explains itself rather than reading as 'no agreement'."""
    notes = [
        f"scanned {consensus_with_verdicts} kept consensus record(s) carrying verdicts; "
        f"{panels_scanned} had >= 2 distinct lineages to compare"
    ]
    if consensus_with_verdicts == 0:
        notes.append(
            "no kept consensus panel carried per-voice verdicts yet -- run a tally-strategy consensus "
            "(strategy=majority / unanimous / ...) with persist=true to build the corpus"
        )
    elif panels_scanned == 0:
        notes.append(
            "kept panels carried verdicts but none had two distinct model lineages to compare "
            "(a single-lineage panel cannot show cross-lineage agreement)"
        )
    notes.append("observational only: agreement is not correctness, so this never down-weights a vote")
    return notes
