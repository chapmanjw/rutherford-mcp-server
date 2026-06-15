# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the durable run ledger writer + reader (F2): state.json + Markdown artifacts."""

from __future__ import annotations

from pathlib import Path

from rutherford.domain.models import RunRecord
from rutherford.io.ledger import RECORD_FILENAME, RunLedger, read_record


def _record(**kwargs: object) -> RunRecord:
    base: dict[str, object] = {"run_id": "abc123", "kind": "delegate", "cli": "fake"}
    base.update(kwargs)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_write_creates_state_and_answer(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(argv=["goose", "acp"], prompt="hello?"), answer="hello world")
    assert run_dir == tmp_path / "jobs" / "abc123"
    assert (run_dir / RECORD_FILENAME).is_file()
    assert (run_dir / "artifacts" / "answer.md").read_text(encoding="utf-8") == "hello world"
    record = read_record(run_dir)  # the record round-trips through the reader the way continuation will read it
    assert record.run_id == "abc123" and record.kind == "delegate" and record.schema_version == 2
    assert record.argv == ["goose", "acp"]
    # env is NEVER persisted -- it can carry API keys; the record has no env field, so it is absent from disk.
    assert "env" not in (run_dir / RECORD_FILENAME).read_text(encoding="utf-8")


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


def test_extra_artifacts_create_nested_files(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(
        _record(),
        answer="x",
        extra_artifacts={"voices/voice-1.md": "# fake\n\n42", "voices/skipped.md": "# Skipped\n\n- dead: down"},
    )
    assert (run_dir / "artifacts" / "voices" / "voice-1.md").read_text(encoding="utf-8") == "# fake\n\n42"
    assert "down" in (run_dir / "artifacts" / "voices" / "skipped.md").read_text(encoding="utf-8")


def test_blank_extra_artifact_writes_a_placeholder(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(), answer="x", extra_artifacts={"voices/voice-1.md": "   "})
    assert (run_dir / "artifacts" / "voices" / "voice-1.md").read_text(encoding="utf-8") == "(empty)"


def test_root_property_exposes_the_jobs_dir(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "jobs")
    assert ledger.root == tmp_path / "jobs"


def test_remove_drops_a_child_but_refuses_to_escape(tmp_path: Path) -> None:
    # remove deletes one run's own directory (the failed-resume probe cleanup, item 9); a malformed id that
    # could reach the root or its parent (``..`` / a separator) is a no-op, so a delete can never escape.
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(_record(), answer="x")
    assert run_dir.exists()
    ledger.remove("abc123")
    assert not run_dir.exists()  # the child was removed
    sentinel = tmp_path / "sentinel"  # a sibling of the jobs root: a ``..`` escape must NOT touch it
    sentinel.mkdir()
    for bad in ("..", ".", "", "../sentinel", "a/b"):
        ledger.remove(bad)
    assert sentinel.exists() and (tmp_path / "jobs").exists()  # nothing outside a direct child was deleted


def test_state_record_round_trips_clean_inputs(tmp_path: Path) -> None:
    # The replay-complete inputs round-trip through the reader: argv (the launch command), prompt, model, cwd.
    # env is absent. This is what continuation recomposes a run from.
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(
        _record(argv=["goose", "acp"], prompt="what is 17 + 25?", model="m", cwd="/work"), answer="x"
    )
    record = read_record(run_dir)
    assert record.argv == ["goose", "acp"]
    assert record.prompt == "what is 17 + 25?"
    assert record.model == "m"
    assert record.cwd == "/work"


def test_state_record_round_trips_a_colon_bearing_argv(tmp_path: Path) -> None:
    # The case the TOON codec could not round-trip (a colon-bearing argv element: a Windows path, an
    # ``ollama run gemma3:12b``). state.json is JSON -- an internal record no LLM consumes -- so it round-trips
    # losslessly, which is what unblocks the F2 reader side (job continuation).
    ledger = RunLedger(tmp_path / "jobs")
    run_dir = ledger.write(
        RunRecord(run_id="x", kind="delegate", cli="ollama", argv=["ollama", "run", "gemma3:12b"]), answer="x"
    )
    assert read_record(run_dir).argv == ["ollama", "run", "gemma3:12b"]
