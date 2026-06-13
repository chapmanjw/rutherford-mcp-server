# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the durable run ledger writer (F2): state.toon + Markdown artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.domain.models import RunRecord
from rutherford.io.ledger import RunLedger
from rutherford.io.serialize import decode, encode


def _record(**kwargs: object) -> RunRecord:
    base: dict[str, object] = {"run_id": "abc123", "kind": "delegate", "cli": "fake"}
    base.update(kwargs)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_write_creates_state_and_answer(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(argv=["fake", "run", "gemma3:12b"]), answer="hello world")
    assert run_dir == tmp_path / "jobs" / "abc123"
    assert (run_dir / "state.toon").is_file()
    assert (run_dir / "artifacts" / "answer.md").read_text(encoding="utf-8") == "hello world"
    # state.toon is the human/LLM-readable record; assert its content as text, not via decode() --
    # python-toon 0.1.3 cannot round-trip an inline array with a quoted element (see RunLedger).
    state = (run_dir / "state.toon").read_text(encoding="utf-8")
    assert "run_id: abc123" in state
    assert "kind: delegate" in state
    assert "schema_version: 1" in state
    assert "gemma3:12b" in state  # the pinned argv element survives the write verbatim


def test_empty_answer_writes_a_placeholder(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(), answer="   ")
    assert (run_dir / "artifacts" / "answer.md").read_text(encoding="utf-8") == "(no answer)"


def test_diff_artifact_written_as_a_fenced_block(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(), answer="x", diff="--- a\n+++ b\n+added line")
    diff_md = (run_dir / "artifacts" / "diff.md").read_text(encoding="utf-8")
    assert "```diff" in diff_md
    assert "+added line" in diff_md


def test_no_diff_artifact_when_absent_or_blank(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(), answer="x", diff="   ")
    assert not (run_dir / "artifacts" / "diff.md").exists()


def test_root_property_exposes_the_jobs_dir(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    assert ledger.root == tmp_path / "jobs"


@pytest.mark.xfail(
    reason="python-toon 0.1.3 cannot round-trip an inline array with quoted elements; the F2 "
    "reader-side roadmap (job continuation) must fix the codec or add a tolerant reader",
    strict=True,
)
def test_state_record_round_trips_a_colon_bearing_argv() -> None:
    # Tracks the documented limitation: a real argv has colon-bearing elements (e.g. gemma3:12b) that
    # get quoted on encode and currently break decode. strict=True, so a future python-toon fix flips
    # this to a loud xpass and signals that the machine reader can be enabled.
    record = RunRecord(run_id="x", kind="delegate", cli="ollama", argv=["ollama", "run", "gemma3:12b"])
    assert decode(encode(record)) is not None
