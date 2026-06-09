# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the LM Studio adapter (local model via ``lms chat <model> -p``)."""

from __future__ import annotations

import pytest

from rutherford.adapters.lmstudio import LMStudioAdapter, _clean_output, _parse_model_keys
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe

_MODEL = "google/gemma-4-12b"


def _ctx(
    model: str | None = _MODEL,
    *,
    role_preamble: str | None = None,
    extra_args: list[str] | None = None,
) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="lmstudio", model=model),
        safety_mode=SafetyMode.READ_ONLY,
        correlation_id="t",
        role_preamble=role_preamble,
        extra_args=extra_args or [],
    )


def _req(model: str | None = _MODEL, *, prompt: str = "hello", files: list[str] | None = None) -> DelegationRequest:
    return DelegationRequest(target=Target(cli="lmstudio", model=model), prompt=prompt, files=files or [])


def test_adapter_is_optional() -> None:
    assert LMStudioAdapter().optional is True


def test_build_invocation_runs_chat_with_prompt_as_argv() -> None:
    spec = LMStudioAdapter().build_invocation(_req(_MODEL, prompt="reverse a string"), _ctx(_MODEL))
    assert spec.argv == ["lms", "chat", _MODEL, "-p", "reverse a string"]
    assert spec.stdin is None  # lms takes the prompt via -p, not stdin


def test_role_preamble_rides_in_system_prompt_flag() -> None:
    # LM Studio has a native -s/--system-prompt flag, so the preamble is NOT prepended to the prompt.
    spec = LMStudioAdapter().build_invocation(_req(prompt="do X"), _ctx(role_preamble="You are a coder."))
    assert spec.argv == ["lms", "chat", _MODEL, "-p", "do X", "-s", "You are a coder."]


def test_build_invocation_appends_extra_args_and_file_context() -> None:
    spec = LMStudioAdapter().build_invocation(_req(prompt="fix it", files=["a.py"]), _ctx(extra_args=["--ttl", "3600"]))
    assert spec.argv[:3] == ["lms", "chat", _MODEL]
    assert spec.argv[-2:] == ["--ttl", "3600"]
    # The in-scope file list is folded into the -p prompt (no file-attach flag).
    prompt = spec.argv[spec.argv.index("-p") + 1]
    assert "fix it" in prompt and "a.py" in prompt


def test_build_invocation_requires_a_model_without_subprocess() -> None:
    probe = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="should not be called"))
    adapter = LMStudioAdapter(probe=probe)
    with pytest.raises(RutherfordError) as info:
        adapter.build_invocation(_req(None), _ctx(None))
    assert info.value.code == ErrorCode.INVALID_INPUT
    assert "default_model" in str(info.value)
    assert probe.calls == []  # build_invocation stays pure -- no `lms ls`


def test_map_safety_is_empty_for_every_mode() -> None:
    adapter = LMStudioAdapter()
    for mode in SafetyMode:
        assert adapter.map_safety(mode).args == []


def test_check_auth_is_authenticated_without_credentials() -> None:
    assert LMStudioAdapter().check_auth().state is AuthState.AUTHENTICATED


def test_detect_version_reads_cli_commit() -> None:
    banner = "\x1b[38;5;166m  __ \x1b[0m\nlms is LM Studio's CLI utility.\nCLI commit: efce996\n"
    probe = FakeProbe(which_map={"lms": "/usr/bin/lms"}, run_fn=lambda argv: ProcessResult(exit_code=0, stdout=banner))
    assert LMStudioAdapter(probe=probe).detect().version == "commit efce996"


def test_available_models_parses_ls_json_llm_only_deduped() -> None:
    listing = (
        '[{"type":"llm","modelKey":"google/gemma-4-12b"},'
        '{"type":"embedding","modelKey":"text-embedding-nomic"},'
        '{"type":"llm","modelKey":"google/gemma-4-31b-qat"},'
        '{"type":"llm","modelKey":"google/gemma-4-12b"}]'  # duplicate (local + remote device)
    )
    adapter = LMStudioAdapter(probe=FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout=listing)))
    assert adapter.available_models() == ["google/gemma-4-12b", "google/gemma-4-31b-qat"]


def test_available_models_empty_when_list_fails() -> None:
    adapter = LMStudioAdapter(probe=FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=1, stderr="boom")))
    assert adapter.available_models() == []


def test_parse_output_strips_load_progress_and_think_block() -> None:
    # Real shape: a carriage-return load bar on stdout, then a <think> block, then the answer.
    raw = (
        "\x1b[?25l\rLoading google/gemma-4-12b 0% ⠋"
        "\rLoading google/gemma-4-12b 100% ⠦\r\x1b[K\x1b[?25h"
        "<think>\n\nThe user wants exactly OK.\n</think>\nOK\n"
    )
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=0, stdout=raw), _ctx())
    assert result.ok
    assert result.text == "OK"


def test_parse_output_think_tag_in_body_is_preserved() -> None:
    # A closed <think>...</think> pair that appears in the MIDDLE of the answer (e.g. an
    # explanation of reasoning models or an XML/regex example) must survive verbatim.
    # The anchored _THINK_RE must not touch it.
    body = "Some intro text.\n<think>example tag</think>\nSome trailing text."
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=0, stdout=body), _ctx())
    assert result.ok
    assert result.text == body.strip()


def test_parse_output_unterminated_think_is_parse_error() -> None:
    # When a reasoning model is truncated mid-thought the closing </think> is absent.
    # lms exits 0, but returning the raw monologue as ok=True is wrong; fail loudly.
    raw = "<think>\nI am reasoning about this forever...\n"
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=0, stdout=raw), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"
    assert "unterminated" in result.error.message


def test_parse_output_plain_answer_passes_through() -> None:
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=0, stdout="PONG\n"), _ctx())
    assert result.ok and result.text == "PONG"


def test_parse_output_empty_is_parse_error() -> None:
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=0, stdout="   "), _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "PARSE_ERROR"


def test_parse_output_nonzero_is_failure() -> None:
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=1, stderr="model not found"), _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "NONZERO_EXIT"


def test_parse_output_timeout() -> None:
    result = LMStudioAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert not result.ok
    assert result.error is not None and result.error.code == "TIMEOUT"


def test_clean_output_renders_carriage_returns_but_keeps_crlf_content() -> None:
    # Bare-CR progress overwrites are collapsed to the last segment; real CRLF content is preserved.
    assert _clean_output("\rprogress 1\rprogress 2\rdone") == "done"
    assert _clean_output("line1\r\nline2\r\n") == "line1\nline2"


def test_parse_model_keys_handles_bad_json() -> None:
    assert _parse_model_keys("not json") == []
    assert _parse_model_keys("{}") == []
