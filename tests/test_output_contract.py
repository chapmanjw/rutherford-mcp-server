# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the output-contract drift canary (CONTRACT_MISMATCH).

Two layers: the central enforcement in the delegation service (driven by a FakeAdapter whose
contract can be flipped), and the per-adapter contract assertions for the two adapters that have
a real machine-readable output shape (Claude's JSON envelope, Codex's JSONL event stream).
"""

from __future__ import annotations

from rutherford.adapters.claude_code import ClaudeCodeAdapter
from rutherford.adapters.codex import CodexAdapter
from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.models import DelegationRequest, ProcessResult, Target
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


def _service(adapter: FakeAdapter, runner: FakeProcessRunner) -> DelegationService:
    return DelegationService(AdapterRegistry([adapter]), runner, RutherfordConfig(), load_roles())


def _req(cli: str = "fake") -> DelegationRequest:
    return DelegationRequest(target=Target(cli=cli), prompt="question")


# --- central enforcement -----------------------------------------------------


async def test_clean_run_with_broken_contract_is_failed_loudly() -> None:
    # A run that exits 0 (so the adapter parses it as ok) but whose output no longer matches the
    # adapter's contract must not be trusted: it is failed with CONTRACT_MISMATCH, not returned ok.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="looks fine but is drifted"))
    result = await _service(FakeAdapter("fake", contract_ok=False), runner).delegate(_req())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "CONTRACT_MISMATCH"


async def test_clean_run_with_satisfied_contract_passes() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="the answer"))
    result = await _service(FakeAdapter("fake", contract_ok=True), runner).delegate(_req())
    assert result.ok
    assert result.text == "the answer"


async def test_failed_run_keeps_its_own_error_code_even_when_contract_is_broken() -> None:
    # The contract is only checked on an ok result. A run that already failed keeps its real error
    # code (here NONZERO_EXIT) and is never relabeled CONTRACT_MISMATCH.
    runner = FakeProcessRunner(ProcessResult(exit_code=2, stdout="", stderr="boom"))
    result = await _service(FakeAdapter("fake", contract_ok=False), runner).delegate(_req())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


# --- per-adapter contracts ---------------------------------------------------


def test_claude_contract_holds_for_a_json_envelope() -> None:
    adapter = ClaudeCodeAdapter()
    raw = ProcessResult(exit_code=0, stdout='{"result": "hi", "session_id": "s1"}')
    assert adapter.check_output_contract(raw) is True


def test_claude_contract_fails_when_no_json_object_is_present() -> None:
    adapter = ClaudeCodeAdapter()
    raw = ProcessResult(exit_code=0, stdout="plain text, no json envelope")
    assert adapter.check_output_contract(raw) is False


def test_codex_contract_holds_for_a_jsonl_event_stream() -> None:
    adapter = CodexAdapter()
    stdout = "\n".join(
        [
            '{"type": "thread.started", "thread_id": "t1"}',
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}',
        ]
    )
    assert adapter.check_output_contract(ProcessResult(exit_code=0, stdout=stdout)) is True


def test_codex_contract_fails_when_no_events_are_emitted() -> None:
    adapter = CodexAdapter()
    raw = ProcessResult(exit_code=0, stdout="not jsonl, just a line of prose")
    assert adapter.check_output_contract(raw) is False


from rutherford.adapters.opencode import OpenCodeAdapter  # noqa: E402


def test_opencode_contract_holds_for_a_jsonl_event_stream() -> None:
    adapter = OpenCodeAdapter()
    stdout = "\n".join(
        [
            '{"type": "text", "sessionID": "s1", "part": {"text": "hello"}}',
            '{"type": "step_finish", "sessionID": "s1", "part": {"tokens": {"input": 10, "output": 5}, "cost": 0.001}}',
        ]
    )
    assert adapter.check_output_contract(ProcessResult(exit_code=0, stdout=stdout)) is True


def test_opencode_contract_fails_when_no_events_are_emitted() -> None:
    adapter = OpenCodeAdapter()
    raw = ProcessResult(exit_code=0, stdout="not jsonl, just a line of prose")
    assert adapter.check_output_contract(raw) is False


from rutherford.adapters.qwen import QwenAdapter  # noqa: E402


def test_qwen_contract_holds_for_a_json_event_array() -> None:
    adapter = QwenAdapter()
    stdout = (
        "["
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}},'
        '{"type":"result","subtype":"success","session_id":"s1","is_error":false,"result":"hi"}'
        "]"
    )
    assert adapter.check_output_contract(ProcessResult(exit_code=0, stdout=stdout)) is True


def test_qwen_contract_fails_when_output_is_prose() -> None:
    adapter = QwenAdapter()
    raw = ProcessResult(exit_code=0, stdout="plain text, not a json array")
    assert adapter.check_output_contract(raw) is False
