# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit and golden tests for the Junie adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.adapters.junie import JunieAdapter
from rutherford.domain.enums import AuthState, Effort, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

SAMPLES = Path(__file__).parent / "parsers" / "junie"


def _sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def _ctx(
    *, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None, effort: Effort | None = None
) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="junie", model="gemini-3-flash-preview"),
        safety_mode=safety,
        correlation_id="test",
        role_preamble=preamble,
        effort=effort,
    )


def _req(**kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli="junie", model="gemini-3-flash-preview"), "prompt": "say hi"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


def test_build_invocation_prompt_rides_on_stdin_not_argv() -> None:
    # Junie requires a real stdin pipe (DEVNULL -> "Incorrect function"); the prompt doubles as it.
    spec = JunieAdapter().build_invocation(_req(prompt="say hi"), _ctx())
    assert spec.stdin == "say hi"
    assert "say hi" not in spec.argv
    assert spec.argv[:5] == ["junie", "--input-format", "text", "--output-format", "json"]
    assert "--skip-update-check" in spec.argv


def test_build_invocation_project_model_resume_effort() -> None:
    spec = JunieAdapter().build_invocation(_req(working_dir="/work", session_id="sess-1"), _ctx(effort=Effort.MEDIUM))
    assert spec.argv[spec.argv.index("--project") + 1] == "/work"
    assert spec.cwd == "/work"
    assert spec.argv[spec.argv.index("--model") + 1] == "gemini-3-flash-preview"
    assert "--resume" in spec.argv
    assert spec.argv[spec.argv.index("--session-id") + 1] == "sess-1"
    assert spec.argv[spec.argv.index("--effort") + 1] == "medium"


def test_build_invocation_folds_role_and_files_into_stdin() -> None:
    spec = JunieAdapter().build_invocation(_req(files=["a.py"]), _ctx(preamble="You are a reviewer."))
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a reviewer.")
    assert "- a.py" in spec.stdin


def test_map_safety_is_best_effort_for_every_mode() -> None:
    adapter = JunieAdapter()
    for mode in SafetyMode:
        assert adapter.map_safety(mode).args == []


def test_map_effort_clamps_xhigh_to_high() -> None:
    assert JunieAdapter().map_effort(Effort.LOW).args == ["--effort", "low"]
    high = JunieAdapter().map_effort(Effort.XHIGH)
    assert high.args == ["--effort", "high"]
    assert high.applied is Effort.HIGH


def test_parse_success_golden_sums_llm_usage() -> None:
    raw = ProcessResult(exit_code=0, stdout=_sample("success.json"), duration_s=70.0)
    result = JunieAdapter().parse_output(raw, _ctx())
    assert result.ok
    assert result.text == "The capital of France is Paris."
    assert result.session_id == "session-260613-143609-5ml0"
    assert result.cost is not None
    assert result.cost.usd == pytest.approx(0.0594549)
    assert result.cost.input_tokens == 66646
    assert result.cost.output_tokens == 7180


def test_parse_nonzero_exit_is_failure() -> None:
    raw = ProcessResult(exit_code=1, stdout="", stderr=_sample("error.txt"), duration_s=0.5)
    result = JunieAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"


def test_parse_timeout_is_timeout_error() -> None:
    result = JunieAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_parse_result_absent_is_parse_error() -> None:
    raw = ProcessResult(exit_code=0, stdout='{"sessionId":"s","changes":[]}', duration_s=0.1)
    result = JunieAdapter().parse_output(raw, _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_check_output_contract() -> None:
    assert JunieAdapter().check_output_contract(ProcessResult(exit_code=0, stdout='{"result":"x"}')) is True
    assert JunieAdapter().check_output_contract(ProcessResult(exit_code=0, stdout="plain")) is False


def test_check_auth_is_unknown() -> None:
    assert JunieAdapter(probe=FakeProbe()).check_auth().state is AuthState.UNKNOWN
