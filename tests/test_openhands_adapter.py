# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the OpenHands adapter."""

from __future__ import annotations

from pathlib import Path

from rutherford.adapters.openhands import OpenHandsAdapter
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "openhands"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="openhands", model=None),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="openhands", model=None), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_basic_shape_task_is_last() -> None:
    spec = OpenHandsAdapter().build_invocation(_req(), _ctx())
    assert spec.argv[:3] == ["openhands", "--headless", "--json"]
    assert spec.argv[-2:] == ["-t", "say hi"]
    assert spec.stdin is None


def test_build_invocation_always_forces_utf8_env() -> None:
    spec = OpenHandsAdapter().build_invocation(_req(), _ctx())
    # Without UTF-8 stdio OpenHands crashes printing glyphs to a cp1252 pipe.
    assert spec.env["PYTHONIOENCODING"] == "utf-8"
    assert spec.env["PYTHONUTF8"] == "1"
    assert spec.env["OPENHANDS_SUPPRESS_BANNER"] == "1"


def test_build_invocation_resume_and_role() -> None:
    spec = OpenHandsAdapter().build_invocation(_req(session_id="conv-1"), _ctx(preamble="You are a reviewer."))
    assert spec.argv[spec.argv.index("--resume") + 1] == "conv-1"
    assert spec.argv[-1].startswith("You are a reviewer.")


def test_map_safety_llm_approve_for_readonly_always_approve_for_write() -> None:
    adapter = OpenHandsAdapter()
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == ["--llm-approve"]
    assert adapter.map_safety(SafetyMode.PROPOSE).args == ["--llm-approve"]
    assert adapter.map_safety(SafetyMode.WRITE).args == ["--always-approve"]
    assert adapter.map_safety(SafetyMode.YOLO).args == ["--always-approve"]


def test_map_safety_fails_closed() -> None:
    class _FutureMode:
        value = "audit"

    assert OpenHandsAdapter().map_safety(_FutureMode()).args == ["--llm-approve"]  # type: ignore[arg-type]


def test_parse_success_golden_skips_ui_noise() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.jsonl"), duration_s=20.0)
    result = OpenHandsAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."  # leading/trailing newlines stripped
    # The dashed UUID from the --resume hint, not the dashless Conversation ID line.
    assert result.session_id == "8a52ba7f-e5fa-466a-8776-a659d8161491"


def test_parse_nonzero_exit_is_failure() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr=_sample("error.txt"), duration_s=0.5)
    result = OpenHandsAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"


def test_parse_timeout_is_timeout_error() -> None:
    result = OpenHandsAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_no_agent_message_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"id":"u1","source":"user","kind":"MessageEvent"}', duration_s=0.1)
    result = OpenHandsAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert (
        OpenHandsAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"kind":"MessageEvent"}')) is True
    )
    assert OpenHandsAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="just UI text")) is False


def test_check_auth_is_unknown() -> None:
    assert OpenHandsAdapter(probe=FakeProbe()).check_auth().state is AuthState.UNKNOWN
