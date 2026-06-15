# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Cross-run F3 (the SOUND part): verdict persistence, the corpus iterator, and the historical-agreement report.

The report is OBSERVATIONAL -- it reports which model lineages tended to agree across the kept consensus
corpus, and NEVER feeds a vote discount. These tests pin: (1) a strategy consensus persists per-voice verdicts
on its parent record (and an all-voices one persists none); (2) a v1 record with no verdicts still reads
(the schema bump is additive); (3) ``iter_records`` skips the unreadable and sorts newest-first; (4) the
agreement math -- cross-lineage tally, same-lineage exclusion, internal-split + unresolved drops, the
abstaining-voice skip, and the empty corpus; (5) the ``analyze`` tool envelope.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import build_app_context
from rutherford.domain.enums import Strategy
from rutherford.domain.models import (
    ConsensusRequest,
    Provenance,
    RunRecord,
    StoredVerdict,
    StrategyResult,
    Target,
    VoiceVerdict,
)
from rutherford.io.ledger import RECORD_FILENAME, RunLedger, iter_records, read_record
from rutherford.io.serialize import decode
from rutherford.services.analysis import HistoricalAgreementService
from rutherford.services.consensus import ConsensusService, _stored_verdicts
from rutherford.services.delegation import DelegationService
from rutherford.tools.analyze import analyze_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
# Fixed-verdict voices with a fixed vendor: each always answers its env verdict, so a persisted strategy
# panel carries a deterministic per-voice verdict to assert.
ALPHA_YES = AgentDescriptor(
    "alpha_yes", "Alpha Yes", _FAKE_CMD, provider="anthropic", env_overrides=(("RUTHERFORD_FAKE_VERDICT", "yes"),)
)
BETA_NO = AgentDescriptor(
    "beta_no", "Beta No", _FAKE_CMD, provider="openai", env_overrides=(("RUTHERFORD_FAKE_VERDICT", "no"),)
)


def _consensus(tmp_path: Path) -> ConsensusService:
    config = RutherfordConfig()
    registry = DescriptorRegistry([ALPHA_YES, BETA_NO])
    ledger = RunLedger(tmp_path / "jobs")
    delegation = DelegationService(registry, config, ledger=ledger, clock=lambda: 1000.0)
    return ConsensusService(delegation, registry, config, ledger=ledger, clock=lambda: 1000.0)


# --- 1. persistence of the verdicts ------------------------------------------


async def test_strategy_consensus_persists_per_voice_verdicts(tmp_path: Path) -> None:
    request = ConsensusRequest(
        targets=[Target(cli="alpha_yes", model="claude-opus-4-8"), Target(cli="beta_no", model="gpt-5")],
        prompt="decide",
        strategy=Strategy.MAJORITY,
        working_dir=str(REPO_ROOT),
        persist=True,
    )
    result = await _consensus(tmp_path).consensus(request)
    assert result.run_dir is not None
    record = read_record(Path(result.run_dir))
    assert record.schema_version == 2
    assert record.verdicts is not None and len(record.verdicts) == 2
    by_cli = {v.cli: v for v in record.verdicts}
    assert by_cli["alpha_yes"].verdict == "yes" and by_cli["alpha_yes"].provider == "anthropic"
    assert by_cli["alpha_yes"].model == "claude-opus-4-8"
    assert by_cli["beta_no"].verdict == "no" and by_cli["beta_no"].provider == "openai"


async def test_all_voices_consensus_persists_no_verdicts(tmp_path: Path) -> None:
    # The all-voices (default) strategy extracts no per-voice verdict, so the parent records ``verdicts=None``.
    request = ConsensusRequest(
        targets=[Target(cli="alpha_yes"), Target(cli="beta_no")],
        prompt="discuss",
        working_dir=str(REPO_ROOT),
        persist=True,
    )
    result = await _consensus(tmp_path).consensus(request)
    assert result.run_dir is not None
    assert read_record(Path(result.run_dir)).verdicts is None


# --- 1b. the verdict projection: tally yes, RANK no --------------------------


def test_stored_verdicts_projects_a_tally_panel() -> None:
    result = StrategyResult(
        strategy=Strategy.MAJORITY,
        outcome="majority",
        decision="yes",
        voices=[
            VoiceVerdict(
                label="a", cli="a", verdict="yes", provenance=Provenance(provider="anthropic", model="claude-opus-4-8")
            )
        ],
    )
    out = _stored_verdicts(result)
    assert out is not None and out[0].verdict == "yes" and out[0].provider == "anthropic"


def test_stored_verdicts_is_none_for_a_rank_panel_without_tokens() -> None:
    # A RANK StrategyResult ranks rather than tallying a verdict token (every voice's ``verdict`` is None), so
    # there is nothing for a cross-run agreement report to read -- it records None, like an all-voices panel.
    result = StrategyResult(
        strategy=Strategy.RANK,
        outcome="ranked",
        voices=[VoiceVerdict(label="a", cli="a", verdict=None, rank=1), VoiceVerdict(label="b", cli="b", rank=2)],
    )
    assert _stored_verdicts(result) is None


# --- 2. backward compatibility: a v1 record (no verdicts) still reads ---------


def test_read_record_back_compat_v1_without_verdicts(tmp_path: Path) -> None:
    # A record written before schema v2 has no ``verdicts`` key; the additive optional field must still validate.
    run_dir = tmp_path / "old"
    run_dir.mkdir()
    (run_dir / RECORD_FILENAME).write_text(
        json.dumps({"schema_version": 1, "run_id": "old", "kind": "consensus", "cli": "a,b", "created_at": 1.0}),
        encoding="utf-8",
    )
    record = read_record(run_dir)
    assert record.schema_version == 1 and record.verdicts is None


# --- 3. the corpus iterator --------------------------------------------------


def _write(ledger: RunLedger, run_id: str, *, created_at: float, kind: str = "consensus", verdicts=None) -> None:
    ledger.write(
        RunRecord(run_id=run_id, kind=kind, cli="panel", created_at=created_at, verdicts=verdicts),
        answer="x",
    )


def test_iter_records_skips_unreadable_and_sorts_newest_first(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    ledger = RunLedger(root)
    _write(ledger, "older", created_at=10.0)
    _write(ledger, "newer", created_at=20.0)
    (root / "broken").mkdir()
    (root / "broken" / RECORD_FILENAME).write_text("{ not json", encoding="utf-8")  # malformed -> skipped
    (root / "stray.txt").write_text("not a run dir", encoding="utf-8")  # a file, not a dir -> skipped
    out = list(iter_records(root))
    assert [record.run_id for _, record in out] == ["newer", "older"]  # only the two valid, newest-first


def test_iter_records_on_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert list(iter_records(tmp_path / "nope")) == []


# --- 4. the historical-agreement report --------------------------------------


def _v(
    label: str, model: str | None, verdict: str | None, *, provider: str | None = None, ok: bool = True
) -> StoredVerdict:
    return StoredVerdict(label=label, cli=label, provider=provider, model=model, verdict=verdict, ok=ok)


def _report(tmp_path: Path, panels: list[tuple[str, list[StoredVerdict]]], **kinds: str):
    """Write each (run_id, verdicts) panel and return the computed report. ``kinds`` overrides a run's kind."""
    ledger = RunLedger(tmp_path / "jobs")
    for index, (run_id, verdicts) in enumerate(panels):
        _write(ledger, run_id, created_at=float(index), kind=kinds.get(run_id, "consensus"), verdicts=verdicts or None)
    return HistoricalAgreementService(ledger).report()


def test_report_tallies_cross_lineage_agreement(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        [
            ("p1", [_v("a", "claude-opus-4-8", "yes"), _v("b", "claude-sonnet-4-6", "yes")]),  # opus vs sonnet AGREE
            ("p2", [_v("a", "claude-opus-4-8", "yes"), _v("b", "claude-sonnet-4-6", "no")]),  # opus vs sonnet DISAGREE
            ("p3", [_v("a", "claude-opus-4-8", "yes"), _v("c", "gpt-5", "yes")]),  # opus vs gpt-5 AGREE
        ],
    )
    pairs = {(p.a, p.b): p for p in report.pairs}
    assert report.panels_scanned == 3
    opus_sonnet = pairs[("claude-opus", "claude-sonnet")]
    assert opus_sonnet.panels == 2 and opus_sonnet.agreements == 1 and opus_sonnet.agreement_rate == 0.5
    opus_gpt = pairs[("claude-opus", "gpt-5")]
    assert opus_gpt.panels == 1 and opus_gpt.agreements == 1 and opus_gpt.agreement_rate == 1.0
    assert report.lineages == ["claude-opus", "claude-sonnet", "gpt-5"]


def test_report_excludes_same_lineage_pairs(tmp_path: Path) -> None:
    # Two opus seats + one sonnet: opus is internally unanimous, so it contributes one verdict; the only pair is
    # the DISTINCT (opus, sonnet) -- never a self (opus, opus) pair.
    report = _report(
        tmp_path,
        [
            (
                "p1",
                [
                    _v("a", "claude-opus-4-8", "yes"),
                    _v("a2", "claude-opus-4-7", "yes"),
                    _v("b", "claude-sonnet-4-6", "yes"),
                ],
            )
        ],
    )
    assert [(p.a, p.b) for p in report.pairs] == [("claude-opus", "claude-sonnet")]
    assert all(p.a != p.b for p in report.pairs)
    assert report.dropped_lineage_panels == 0  # the two opus seats agreed -- the lineage was not dropped


def test_report_drops_an_internally_split_lineage(tmp_path: Path) -> None:
    # Two opus seats DISAGREE with each other: opus has no single panel verdict, so it is dropped (counted),
    # leaving only sonnet -> a single lineage -> no pair, panel not scanned.
    report = _report(
        tmp_path,
        [
            (
                "p1",
                [
                    _v("a", "claude-opus-4-8", "yes"),
                    _v("a2", "claude-opus-4-7", "no"),
                    _v("b", "claude-sonnet-4-6", "yes"),
                ],
            )
        ],
    )
    assert report.pairs == []
    assert report.panels_scanned == 0
    assert report.dropped_lineage_panels == 1


def test_report_drops_an_unresolved_provenance_voice(tmp_path: Path) -> None:
    # A voice whose provenance resolves to no lineage (no provider, no recognized model) is dropped + counted;
    # only opus remains -> no pair.
    report = _report(
        tmp_path,
        [("p1", [_v("a", "claude-opus-4-8", "yes"), _v("x", None, "yes", provider=None)])],
    )
    assert report.pairs == []
    assert report.panels_scanned == 0
    assert report.dropped_lineage_panels == 1


def test_report_skips_an_abstaining_voice_without_dropping_a_lineage(tmp_path: Path) -> None:
    # A failed / unparseable voice (no verdict) is skipped silently -- it never had a verdict to compare, so it
    # is NOT a dropped lineage. opus + gpt-5 still pair and agree.
    report = _report(
        tmp_path,
        [
            (
                "p1",
                [
                    _v("a", "claude-opus-4-8", "yes"),
                    _v("b", "claude-sonnet-4-6", None, ok=False),  # failed: no verdict
                    _v("c", "gpt-5", "yes"),
                ],
            )
        ],
    )
    assert [(p.a, p.b) for p in report.pairs] == [("claude-opus", "gpt-5")]
    assert report.dropped_lineage_panels == 0  # the abstaining voice is not a dropped lineage


def test_report_ignores_non_consensus_and_verdictless_records(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        [
            ("p1", [_v("a", "claude-opus-4-8", "yes"), _v("b", "claude-sonnet-4-6", "yes")]),  # counts
            ("d1", [_v("a", "claude-opus-4-8", "yes"), _v("b", "gpt-5", "yes")]),  # a DEBATE -> ignored
            ("none", []),  # a consensus with no verdicts (all-voices) -> ignored
        ],
        d1="debate",
    )
    assert report.panels_scanned == 1
    assert [(p.a, p.b) for p in report.pairs] == [("claude-opus", "claude-sonnet")]


def test_report_on_an_empty_corpus_explains_itself(tmp_path: Path) -> None:
    report = HistoricalAgreementService(RunLedger(tmp_path / "jobs")).report()
    assert report.panels_scanned == 0 and report.pairs == []
    assert any("persist=true" in note for note in report.notes)  # tells the caller how to build a corpus
    assert any("never down-weights a vote" in note for note in report.notes)  # the soundness disclaimer rides along


# --- 5. the analyze tool envelope --------------------------------------------


async def test_analyze_tool_returns_the_report(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    _write(ledger, "p1", created_at=1.0, verdicts=[_v("a", "claude-opus-4-8", "yes"), _v("b", "gpt-5", "yes")])
    app = build_app_context(
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")), descriptors=DescriptorRegistry([ALPHA_YES])
    )
    payload = decode(await analyze_tool(app))
    assert payload["panels_scanned"] == 1
    assert payload["pairs"][0]["a"] == "claude-opus" and payload["pairs"][0]["b"] == "gpt-5"


async def test_analyze_tool_rejects_an_unknown_report(tmp_path: Path) -> None:
    app = build_app_context(
        config=RutherfordConfig(jobs_dir=str(tmp_path / "jobs")), descriptors=DescriptorRegistry([ALPHA_YES])
    )
    payload = decode(await analyze_tool(app, report="bogus"))
    assert payload["error"]["code"] == "INVALID_INPUT"
