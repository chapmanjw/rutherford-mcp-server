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


def _write_transcript(root: Path, conv_id: str, lines: list[dict[str, Any]], workspace: Path | None = None) -> None:
    logs = root / "brain" / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "transcript.jsonl").write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    if workspace is not None:
        cache = root / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "last_conversations.json").write_text(json.dumps({str(workspace): conv_id}), encoding="utf-8")


def _ctx(working_dir: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="antigravity"),
        safety_mode=SafetyMode.READ_ONLY,
        working_dir=working_dir,
        correlation_id="t",
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


def test_parse_timeout(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(data_root=tmp_path / "empty")
    result = adapter.parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_build_invocation_has_no_model_flag() -> None:
    spec = AntigravityAdapter().build_invocation(
        DelegationRequest(target=Target(cli="antigravity", model="ignored"), prompt="hi", working_dir="/w"),
        _ctx("/w"),
    )
    assert spec.argv[:3] == ["agy", "-p", "hi"]
    assert "--model" not in spec.argv
    assert "--add-dir" in spec.argv


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
