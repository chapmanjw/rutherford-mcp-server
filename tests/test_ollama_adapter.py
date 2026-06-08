# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the Ollama adapter (local model via ``ollama run <model>``)."""

from __future__ import annotations

import pytest

from rutherford.adapters.ollama import OllamaAdapter, _parse_model_names
from rutherford.domain.enums import AuthState, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationRequest, InvocationContext, ProcessResult, Target
from tests.fakes import FakeProbe


def _ctx(
    model: str | None = "coder-next",
    *,
    role_preamble: str | None = None,
    extra_args: list[str] | None = None,
) -> InvocationContext:
    return InvocationContext(
        target=Target(cli="ollama", model=model),
        safety_mode=SafetyMode.READ_ONLY,
        correlation_id="t",
        role_preamble=role_preamble,
        extra_args=extra_args or [],
    )


def _req(model: str | None = "coder-next", *, prompt: str = "hello") -> DelegationRequest:
    return DelegationRequest(target=Target(cli="ollama", model=model), prompt=prompt)


def test_adapter_is_optional() -> None:
    assert OllamaAdapter().optional is True


def test_build_invocation_runs_named_model_with_prompt_on_stdin() -> None:
    adapter = OllamaAdapter()
    spec = adapter.build_invocation(_req("coder-next", prompt="reverse a string"), _ctx("coder-next"))
    # --hidethinking keeps a reasoning model's trace out of stdout; the prompt rides on stdin.
    assert spec.argv == ["ollama", "run", "coder-next", "--hidethinking"]
    assert spec.stdin == "reverse a string"
    assert "reverse a string" not in spec.argv  # never concatenated into the command line


def test_build_invocation_appends_configured_extra_args() -> None:
    # ``[adapters.ollama] extra_args`` the service resolved (e.g. --keepalive) are appended verbatim.
    adapter = OllamaAdapter()
    spec = adapter.build_invocation(_req("coder-next"), _ctx("coder-next", extra_args=["--keepalive", "30s"]))
    assert spec.argv == ["ollama", "run", "coder-next", "--hidethinking", "--keepalive", "30s"]


def test_build_invocation_is_pure_no_subprocess_when_model_missing() -> None:
    # With no model resolvable the adapter refuses with INVALID_INPUT, pointing at default_model.
    # It must NOT shell out to `ollama list` to do so (build_invocation stays pure).
    probe = FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout="should not be called"))
    adapter = OllamaAdapter(probe=probe)
    with pytest.raises(RutherfordError) as info:
        adapter.build_invocation(_req(None), _ctx(None))
    assert info.value.code == ErrorCode.INVALID_INPUT
    assert "default_model" in str(info.value)
    assert probe.calls == []  # no `ollama list` on the build path


def test_detect_version_reads_running_version() -> None:
    out = "ollama version is 0.5.7\n"
    probe = FakeProbe(
        which_map={"ollama": "/usr/bin/ollama"}, run_fn=lambda argv: ProcessResult(exit_code=0, stdout=out)
    )
    assert OllamaAdapter(probe=probe).detect().version == "0.5.7"


def test_detect_version_ignores_daemon_down_warning() -> None:
    # `ollama --version` exits 0 with the daemon down but prints a warning line first; the adapter
    # reports the client version, never the warning string.
    out = "Warning: could not connect to a running Ollama instance\nWarning: client version is 0.5.7\n"
    probe = FakeProbe(
        which_map={"ollama": "/usr/bin/ollama"}, run_fn=lambda argv: ProcessResult(exit_code=0, stdout=out)
    )
    adapter = OllamaAdapter(probe=probe)
    assert adapter.detect().version == "0.5.7"


def test_role_preamble_is_prepended_to_the_prompt() -> None:
    adapter = OllamaAdapter()
    spec = adapter.build_invocation(_req(prompt="do X"), _ctx(role_preamble="You are a coder."))
    assert spec.stdin is not None
    assert spec.stdin.startswith("You are a coder.")
    assert "do X" in spec.stdin


def test_map_safety_is_empty_for_every_mode() -> None:
    adapter = OllamaAdapter()
    for mode in SafetyMode:
        flags = adapter.map_safety(mode)
        assert flags.args == []


def test_check_auth_is_authenticated_without_credentials() -> None:
    assert OllamaAdapter().check_auth().state is AuthState.AUTHENTICATED


def test_available_models_parses_ollama_list() -> None:
    listing = "NAME              ID    SIZE\ncoder-next:latest abc   51 GB\nqwen3-research    def   35 GB\n"
    adapter = OllamaAdapter(probe=FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=0, stdout=listing)))
    assert adapter.available_models() == ["coder-next:latest", "qwen3-research"]


def test_available_models_empty_when_list_fails() -> None:
    adapter = OllamaAdapter(probe=FakeProbe(run_fn=lambda argv: ProcessResult(exit_code=1, stderr="daemon down")))
    assert adapter.available_models() == []


def test_parse_output_success() -> None:
    result = OllamaAdapter().parse_output(ProcessResult(exit_code=0, stdout="PONG\n"), _ctx())
    assert result.ok
    assert result.text == "PONG"


def test_parse_output_empty_is_parse_error() -> None:
    result = OllamaAdapter().parse_output(ProcessResult(exit_code=0, stdout="   "), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "PARSE_ERROR"


def test_parse_output_nonzero_is_failure() -> None:
    result = OllamaAdapter().parse_output(ProcessResult(exit_code=1, stderr="model not found"), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


def test_parse_output_timeout() -> None:
    result = OllamaAdapter().parse_output(ProcessResult(exit_code=None, timed_out=True), _ctx())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


def test_parse_model_names_skips_header_and_blanks() -> None:
    assert _parse_model_names("NAME ID SIZE\nfoo a 1\n\nbar b 2\n") == ["foo", "bar"]
    assert _parse_model_names("") == []
