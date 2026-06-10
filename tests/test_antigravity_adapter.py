# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the Antigravity adapter, including the transcript-file quirk."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rutherford.adapters.antigravity import AntigravityAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from rutherford.tools.probing import probe_adapter
from tests.fakes import FakeProbe


def _write_transcript(root: Path, conv_id: str, lines: list[dict[str, Any]], workspace: Path | None = None) -> None:
    logs = root / "brain" / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "transcript.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    if workspace is not None:
        cache = root / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "last_conversations.json").write_text(json.dumps({str(workspace): conv_id}), encoding="utf-8")


def _ctx(working_dir: str | None = None, session_id: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="antigravity"),
        safety_mode=SafetyMode.READ_ONLY,
        working_dir=working_dir,
        correlation_id="t",
        session_id=session_id,
    )


_LINES = [
    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "do the thing"},
    {"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "intermediate step"},
    {"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "THE FINAL ANSWER"},
]


def test_parse_reads_transcript_via_index(tmp_path: Path) -> None:
    root = tmp_path / "agdata"
    workspace = tmp_path / "proj"
    workspace.mkdir()
    _write_transcript(root, "conv-1", _LINES, workspace=workspace)

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout="(unreliable stdout)"), _ctx(str(workspace)))
    assert result.ok
    assert result.text == "THE FINAL ANSWER"
    assert result.session_id == "conv-1"


def test_parse_falls_back_to_newest_brain_dir(tmp_path: Path) -> None:
    root = tmp_path / "agdata"
    _write_transcript(
        root, "older", [{"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "old"}]
    )
    _write_transcript(root, "newer", _LINES)
    # Make "newer" the most recently modified brain entry.
    os.utime(root / "brain" / "older", (1000, 1000))
    os.utime(root / "brain" / "newer", (2000, 2000))

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx())
    assert result.ok
    assert result.text == "THE FINAL ANSWER"
    assert result.session_id == "newer"


def test_parse_transcript_not_found(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(data_root=tmp_path / "empty")
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_NOT_FOUND"


def test_parse_no_transcript_nonempty_stdout_is_error(tmp_path: Path) -> None:
    """Bug 1 regression: exit_code=0 with no readable transcript must fail, even with stdout text.

    The old code returned ok=True using raw.stdout as the answer.  Stdout is documented-unreliable
    (banners, progress bars, ANSI), so a missing transcript must always be TRANSCRIPT_NOT_FOUND.
    The ANSI-stripped stdout is surfaced only as debug ``text`` on the error result.
    """
    adapter = AntigravityAdapter(data_root=tmp_path / "empty")
    stdout_text = "\x1b[32mRunning...\x1b[0m some banner text"
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=stdout_text), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_NOT_FOUND"
    # ANSI-stripped stdout must appear in debug text, not as the successful answer
    assert "Running..." in result.text
    assert "\x1b" not in result.text


def test_parse_working_dir_not_in_index_does_not_use_other_conversation(tmp_path: Path) -> None:
    """Bug 2 regression: working_dir provided but absent from index must not return another
    conversation's answer.

    The old code fell back to _newest_brain_entry() unconditionally, which could return a well-
    formed transcript from a completely different run.
    """
    root = tmp_path / "agdata"
    # Write a transcript for a different conversation (no workspace mapping for our working_dir).
    _write_transcript(root, "other-conv", _LINES)

    our_workspace = tmp_path / "our-proj"
    our_workspace.mkdir()
    # Deliberately do NOT register our_workspace in last_conversations.json.

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx(str(our_workspace)))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_NOT_FOUND"


def test_parse_timeout(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(data_root=tmp_path / "empty")
    result = adapter.parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_parse_transcript_schema_drift_is_contract_mismatch(tmp_path: Path) -> None:
    """A transcript that exists and holds events but matches none of the expected shape is a schema
    drift (the reverse-engineered format changed under a new agy), not a missing transcript -- so it
    fails loudly as CONTRACT_MISMATCH, which F7 makes retryable and counts toward cooldown."""
    root = tmp_path / "agdata"
    workspace = tmp_path / "proj"
    workspace.mkdir()
    # Events present, but the answer type was (hypothetically) renamed -- nothing matches PLANNER_RESPONSE.
    drifted = [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "q"},
        {"source": "MODEL", "type": "ASSISTANT_MESSAGE", "status": "DONE", "content": "answer in a new shape"},
    ]
    _write_transcript(root, "conv-x", drifted, workspace=workspace)

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx(str(workspace)))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "CONTRACT_MISMATCH"


def test_parse_garbage_transcript_is_transcript_not_found(tmp_path: Path) -> None:
    """A resolved transcript with no parseable JSON events at all (corrupt/empty) is treated as an
    absent transcript, not a schema drift -- there is no evidence the schema *changed*."""
    root = tmp_path / "agdata"
    workspace = tmp_path / "proj"
    workspace.mkdir()
    logs = root / "brain" / "conv-g" / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "transcript.jsonl").write_text("not json at all\nstill not json", encoding="utf-8")
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "last_conversations.json").write_text(json.dumps({str(workspace): "conv-g"}), encoding="utf-8")

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx(str(workspace)))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_NOT_FOUND"


def test_parse_partial_in_progress_transcript_is_not_a_drift(tmp_path: Path) -> None:
    """A transcript whose model turn is still IN_PROGRESS (no status=DONE) has no completed turn, so it
    is "no answer yet" (TRANSCRIPT_NOT_FOUND), NOT a schema drift -- this is the false positive the
    coarse "saw any event" predicate would have benched a healthy agy for."""
    root = tmp_path / "agdata"
    workspace = tmp_path / "proj"
    workspace.mkdir()
    partial = [
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "q"},
        {"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "IN_PROGRESS", "content": "thinking..."},
    ]
    _write_transcript(root, "conv-p", partial, workspace=workspace)

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx(str(workspace)))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_NOT_FOUND"


def test_parse_completed_but_empty_answer_is_not_a_drift(tmp_path: Path) -> None:
    """A completed PLANNER_RESPONSE with empty content has the right type (not a schema change) but no
    usable answer -- TRANSCRIPT_NOT_FOUND, not CONTRACT_MISMATCH."""
    root = tmp_path / "agdata"
    workspace = tmp_path / "proj"
    workspace.mkdir()
    _write_transcript(
        root,
        "conv-e",
        [{"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "   "}],
        workspace=workspace,
    )

    adapter = AntigravityAdapter(data_root=root)
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx(str(workspace)))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_NOT_FOUND"


def test_resume_reads_the_session_id_conversation_not_the_newest(tmp_path: Path) -> None:
    """On a resumed run the explicit session_id (the conversation agy was told to continue) is
    authoritative -- it must be read directly, not re-guessed via the newest-brain heuristic, which
    could return a different, more recent conversation's answer."""
    root = tmp_path / "agdata"
    _write_transcript(root, "resume-me", _LINES)
    _write_transcript(
        root, "newer-other", [{"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "WRONG"}]
    )
    os.utime(root / "brain" / "resume-me", (1000, 1000))
    os.utime(root / "brain" / "newer-other", (2000, 2000))  # the newest -- the heuristic would pick this

    adapter = AntigravityAdapter(data_root=root)
    # No working_dir, but an explicit session_id: read resume-me, not the newer-other newest entry.
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout=""), _ctx(session_id="resume-me"))
    assert result.ok
    assert result.text == "THE FINAL ANSWER"
    assert result.session_id == "resume-me"


def _agy_probe(version: str) -> FakeProbe:
    return FakeProbe(
        which_map={"agy": "/usr/bin/agy"},
        run_fn=lambda argv: ProcessResult(exit_code=0, stdout=version),
    )


def test_doctor_flags_agy_version_drift() -> None:
    # agy auto-updates; when the running version is past the verified pin, doctor surfaces a note.
    status = probe_adapter(AntigravityAdapter(probe=_agy_probe("1.0.9")), diagnostic=True)
    assert status.version == "1.0.9"
    assert any("verified against 1.0.7" in note for note in status.notes)


def test_doctor_no_drift_note_when_version_matches_the_pin() -> None:
    status = probe_adapter(AntigravityAdapter(probe=_agy_probe("1.0.7")), diagnostic=True)
    assert not any("verified against" in note for note in status.notes)


def test_doctor_version_drift_compares_the_semver_token_not_the_raw_line() -> None:
    # agy --version may carry extra text; a bump is detected from the extracted semver token.
    status = probe_adapter(AntigravityAdapter(probe=_agy_probe("agy version 1.0.9 (build abc123)")), diagnostic=True)
    assert any("verified against 1.0.7" in note for note in status.notes)


def test_doctor_no_drift_when_only_the_version_string_format_changed() -> None:
    # A format change with the SAME semver (e.g. "agy 1.0.7 (stable)") must not read as a version bump.
    status = probe_adapter(AntigravityAdapter(probe=_agy_probe("agy 1.0.7 (stable)")), diagnostic=True)
    assert not any("verified against" in note for note in status.notes)


def test_build_invocation_has_no_model_flag() -> None:
    spec = AntigravityAdapter().build_invocation(
        DelegationRequest(target=Target(cli="antigravity", model="ignored"), prompt="hi", working_dir="/w"),
        _ctx("/w"),
    )
    assert spec.argv[:3] == ["agy", "-p", "hi"]
    assert "--model" not in spec.argv
    assert "--add-dir" in spec.argv


def test_build_invocation_sets_print_timeout_below_the_kill_deadline() -> None:
    # agy's own --print-timeout is set _PRINT_TIMEOUT_GRACE_S below the runner's hard tree-kill, so a
    # slow run gives up and flushes its final transcript line instead of being killed mid-write.
    spec = AntigravityAdapter().build_invocation(
        DelegationRequest(target=Target(cli="antigravity"), prompt="hi", timeout_s=300),
        _ctx(),
    )
    assert "--print-timeout" in spec.argv
    value = spec.argv[spec.argv.index("--print-timeout") + 1]
    assert value == f"{300 - AntigravityAdapter._PRINT_TIMEOUT_GRACE_S}s"  # agy gives up at 290s, 10s under the kill


def test_build_invocation_print_timeout_floors_at_one_second() -> None:
    # A call timeout at or below the grace would make agy's timeout zero/negative; it floors at 1s.
    spec = AntigravityAdapter().build_invocation(
        DelegationRequest(target=Target(cli="antigravity"), prompt="hi", timeout_s=3),
        _ctx(),
    )
    assert spec.argv[spec.argv.index("--print-timeout") + 1] == "1s"


def test_build_invocation_passes_session_id_as_conversation() -> None:
    # A resumed delegation threads its session_id to agy as --conversation, so agy continues the right
    # brain/ conversation (and parse_output then reads that same conversation's transcript).
    spec = AntigravityAdapter().build_invocation(
        DelegationRequest(target=Target(cli="antigravity"), prompt="hi", session_id="conv-123"),
        _ctx(session_id="conv-123"),
    )
    assert "--conversation" in spec.argv
    assert spec.argv[spec.argv.index("--conversation") + 1] == "conv-123"


def test_build_invocation_omits_conversation_without_a_session_id() -> None:
    spec = AntigravityAdapter().build_invocation(
        DelegationRequest(target=Target(cli="antigravity"), prompt="hi"),
        _ctx(),
    )
    assert "--conversation" not in spec.argv


def test_map_safety() -> None:
    adapter = AntigravityAdapter()
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == []
    assert "--dangerously-skip-permissions" in adapter.map_safety(SafetyMode.WRITE).args
    assert "--dangerously-skip-permissions" in adapter.map_safety(SafetyMode.YOLO).args


def test_no_models() -> None:
    assert AntigravityAdapter().available_models() == []


def test_check_auth_is_unknown() -> None:
    # agy has no non-interactive whoami and no reliable cross-platform on-disk marker, so a cheap
    # probe cannot determine auth state. doctor resolves this with a live round trip.
    assert AntigravityAdapter().check_auth().state is AuthState.UNKNOWN
