# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the config-driven generic adapter."""

from __future__ import annotations

from typing import Any

import pytest

from rutherford.adapters.generic import GenericAdapter
from rutherford.config.schema import GenericAdapterConfig, GenericSafetyConfig
from rutherford.domain.enums import AuthState, OutputMode, SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target


def _cfg(**kwargs: Any) -> GenericAdapterConfig:
    base: dict[str, Any] = {"id": "mycli", "display_name": "My CLI", "binary": "mycli", "base_args": ["run"]}
    base.update(kwargs)
    return GenericAdapterConfig(**base)


def _ctx(*, safety: SafetyMode = SafetyMode.READ_ONLY, preamble: str | None = None) -> InvocationContext:
    return InvocationContext(target=Target(cli="mycli"), safety_mode=safety, role_preamble=preamble, correlation_id="t")


def _req(**kwargs: Any) -> DelegationRequest:
    base: dict[str, Any] = {"target": Target(cli="mycli", model="m"), "prompt": "hi"}
    base.update(kwargs)
    return DelegationRequest(**base)


def test_build_invocation_positional_prompt() -> None:
    adapter = GenericAdapter(_cfg(model_flag="--model", working_dir_flag="--dir"))
    spec = adapter.build_invocation(_req(working_dir="/w"), _ctx())
    assert spec.argv[0] == "mycli"
    assert "run" in spec.argv
    assert spec.argv[-1] == "hi"
    assert spec.argv[spec.argv.index("--model") + 1] == "m"
    assert spec.argv[spec.argv.index("--dir") + 1] == "/w"
    assert spec.stdin is None


def test_build_invocation_stdin_prompt() -> None:
    adapter = GenericAdapter(_cfg(prompt_on_stdin=True))
    spec = adapter.build_invocation(_req(), _ctx())
    assert spec.stdin == "hi"
    assert "hi" not in spec.argv


def test_build_invocation_includes_role_and_files() -> None:
    adapter = GenericAdapter(_cfg())
    spec = adapter.build_invocation(_req(files=["a.py"]), _ctx(preamble="be terse"))
    prompt = spec.argv[-1]
    assert "be terse" in prompt
    assert "a.py" in prompt


def test_map_safety_from_config() -> None:
    adapter = GenericAdapter(_cfg(safety=GenericSafetyConfig(read_only=["--ro"], write=["--rw"], yolo=["--yolo"])))
    assert adapter.map_safety(SafetyMode.READ_ONLY).args == ["--ro"]
    assert adapter.map_safety(SafetyMode.WRITE).args == ["--rw"]
    assert adapter.map_safety(SafetyMode.YOLO).args == ["--yolo"]
    assert adapter.map_safety(SafetyMode.PROPOSE).args == []


def test_parse_text_mode() -> None:
    adapter = GenericAdapter(_cfg(output_mode=OutputMode.TEXT))
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout="  answer  "), _ctx())
    assert result.ok
    assert result.text == "answer"


def test_parse_json_path() -> None:
    adapter = GenericAdapter(_cfg(output_mode=OutputMode.JSON, json_text_path="result.text"))
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout='{"result":{"text":"hello"}}'), _ctx())
    assert result.ok
    assert result.text == "hello"


def test_parse_json_missing_path_is_parse_error() -> None:
    adapter = GenericAdapter(_cfg(output_mode=OutputMode.JSON, json_text_path="nope"))
    result = adapter.parse_output(ProcessResult(exit_code=0, stdout='{"result":"x"}'), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_nonzero_and_timeout() -> None:
    adapter = GenericAdapter(_cfg())
    nonzero = adapter.parse_output(ProcessResult(exit_code=1, stderr="bad"), _ctx())
    assert nonzero.error is not None and nonzero.error.code == "NONZERO_EXIT"
    timed = adapter.parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert timed.error is not None and timed.error.code == "TIMEOUT"


def test_capabilities_reflect_config() -> None:
    adapter = GenericAdapter(_cfg(model_flag="--model", working_dir_flag="--dir", output_mode=OutputMode.JSON))
    caps = adapter.capabilities()
    assert caps.supports_model_selection
    assert caps.supports_working_dir
    assert caps.output_mode is OutputMode.JSON


def test_check_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = GenericAdapter(_cfg(auth_env=["MYCLI_KEY"]))
    monkeypatch.delenv("MYCLI_KEY", raising=False)
    assert adapter.check_auth().state is AuthState.API_KEY_MISSING
    monkeypatch.setenv("MYCLI_KEY", "x")
    assert adapter.check_auth().state is AuthState.AUTHENTICATED


def test_check_auth_unknown_without_env() -> None:
    assert GenericAdapter(_cfg()).check_auth().state is AuthState.UNKNOWN
