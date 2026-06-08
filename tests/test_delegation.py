# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation service, driven entirely by fakes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import AdapterConfig, RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import DelegationRequest, ProcessResult, Target
from rutherford.runtime.depth import ENV_DEPTH
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


def _service(
    adapters: Sequence[FakeAdapter],
    runner: FakeProcessRunner,
    config: RutherfordConfig | None = None,
) -> DelegationService:
    return DelegationService(
        AdapterRegistry(list(adapters)),
        runner,
        config or RutherfordConfig(),
        load_roles(),
    )


def _req(cli: str = "fake", **kwargs: object) -> DelegationRequest:
    base: dict[str, object] = {"target": Target(cli=cli), "prompt": "question"}
    base.update(kwargs)
    return DelegationRequest(**base)  # type: ignore[arg-type]


async def test_successful_delegation_overlays_depth_env() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="the answer"))
    result = await _service([FakeAdapter("fake")], runner).delegate(_req(), base_depth=0)
    assert result.ok
    assert result.text == "the answer"
    spec, _timeout = runner.calls[0]
    assert spec.env[ENV_DEPTH] == "1"


async def test_configured_default_model_fills_in_when_call_names_none() -> None:
    # `[adapters.fake] default_model` is honored before the adapter builds the invocation, so a call
    # that names no model runs against the configured one.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(adapters={"fake": AdapterConfig(default_model="m9")})
    await _service([FakeAdapter("fake")], runner, config).delegate(_req())
    spec, _ = runner.calls[0]
    assert "--model" in spec.argv and "m9" in spec.argv


async def test_nonzero_exit_is_a_failed_result() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=2, stdout="", stderr="boom"))
    result = await _service([FakeAdapter("fake")], runner).delegate(_req())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "NONZERO_EXIT"


async def test_timeout_is_a_failed_result() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=None, timed_out=True))
    result = await _service([FakeAdapter("fake")], runner).delegate(_req())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "TIMEOUT"


async def test_unknown_target_does_not_spawn() -> None:
    runner = FakeProcessRunner()
    result = await _service([FakeAdapter("fake")], runner).delegate(_req(cli="ghost"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "UNKNOWN_TARGET"
    assert runner.calls == []


async def test_missing_binary_does_not_spawn() -> None:
    runner = FakeProcessRunner()
    service = _service([FakeAdapter("fake", installed=False)], runner)
    result = await service.delegate(_req())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "BINARY_NOT_FOUND"
    assert runner.calls == []


async def test_self_referential_chain_stops_at_max_depth() -> None:
    # The caller-agnostic guarantee in test form: a CLI delegating to its own adapter is bounded.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _service([FakeAdapter("claude_code")], runner, RutherfordConfig(max_depth=2))
    req = DelegationRequest(target=Target(cli="claude_code"), prompt="delegate to yourself")

    assert (await service.delegate(req, base_depth=0)).ok
    assert (await service.delegate(req, base_depth=1)).ok
    refused = await service.delegate(req, base_depth=2)

    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code == "MAX_DEPTH_EXCEEDED"
    assert len(runner.calls) == 2  # depth 2 was refused without spawning


async def test_write_mode_blocked_without_trusted_workspace() -> None:
    runner = FakeProcessRunner()
    result = await _service([FakeAdapter("fake")], runner).delegate(
        _req(safety_mode=SafetyMode.WRITE, working_dir="/some/dir"),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "WORKSPACE_NOT_TRUSTED"
    assert runner.calls == []


async def test_write_mode_allowed_with_per_call_confirmation() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="done"))
    result = await _service([FakeAdapter("fake")], runner).delegate(
        _req(safety_mode=SafetyMode.WRITE, working_dir="/some/dir", trust_workspace=True),
    )
    assert result.ok


async def test_write_mode_allowed_when_under_allowlist(tmp_path: Path) -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="done"))
    config = RutherfordConfig(trusted_workspaces=[str(tmp_path)])
    workdir = tmp_path / "project"
    workdir.mkdir()
    result = await _service([FakeAdapter("fake")], runner, config).delegate(
        _req(safety_mode=SafetyMode.WRITE, working_dir=str(workdir)),
    )
    assert result.ok


async def test_role_preamble_is_injected() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="planned"))
    await _service([FakeAdapter("fake")], runner).delegate(_req(role="planner"))
    spec, _timeout = runner.calls[0]
    assert "planning specialist" in spec.argv[2]


async def test_unknown_role_is_a_failed_result() -> None:
    runner = FakeProcessRunner()
    result = await _service([FakeAdapter("fake")], runner).delegate(_req(role="ghost"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "ROLE_NOT_FOUND"


async def test_include_raw_controls_raw_field() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="hi", stderr="note"))
    with_raw = await _service([FakeAdapter("fake")], runner).delegate(_req(include_raw=True))
    assert with_raw.raw is not None
    runner2 = FakeProcessRunner(ProcessResult(exit_code=0, stdout="hi"))
    without_raw = await _service([FakeAdapter("fake")], runner2).delegate(_req(include_raw=False))
    assert without_raw.raw is None


async def test_safety_flags_reach_the_invocation() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    await _service([FakeAdapter("fake")], runner).delegate(
        _req(safety_mode=SafetyMode.YOLO, working_dir="/x", trust_workspace=True),
    )
    spec, _timeout = runner.calls[0]
    assert "--safety=yolo" in spec.argv


# --- per-target model fallback ------------------------------------------------------------------


def _model_unavailable_run_fn() -> object:
    """A runner fn that rejects the ``named-only`` model and answers on anything else."""

    def run_fn(spec: object) -> ProcessResult:
        if "named-only" in spec.argv:  # type: ignore[attr-defined]
            return ProcessResult(exit_code=1, stderr="Named models unavailable on your plan. Switch to Auto.")
        return ProcessResult(exit_code=0, stdout="answered on auto")

    return run_fn


async def test_model_fallback_retries_with_fallback_model() -> None:
    runner = FakeProcessRunner(run_fn=_model_unavailable_run_fn())  # type: ignore[arg-type]
    adapter = FakeAdapter("cursorish", fallback_model="auto")
    result = await _service([adapter], runner).delegate(_req(target=Target(cli="cursorish", model="named-only")))
    assert result.ok
    assert result.text == "answered on auto"
    assert result.fallback_from == "named-only"  # the originally requested model that was rejected
    assert result.target.model == "auto"  # the model that actually answered
    assert len(runner.calls) == 2  # one original attempt + one fallback retry


async def test_no_fallback_without_a_fallback_model() -> None:
    runner = FakeProcessRunner(run_fn=_model_unavailable_run_fn())  # type: ignore[arg-type]
    adapter = FakeAdapter("plain")  # fallback_model defaults to None
    result = await _service([adapter], runner).delegate(_req(target=Target(cli="plain", model="named-only")))
    assert not result.ok
    assert result.fallback_from is None
    assert len(runner.calls) == 1  # nothing to retry with


async def test_no_fallback_when_disabled() -> None:
    runner = FakeProcessRunner(run_fn=_model_unavailable_run_fn())  # type: ignore[arg-type]
    adapter = FakeAdapter("cursorish", fallback_model="auto")
    result = await _service([adapter], runner).delegate(
        _req(target=Target(cli="cursorish", model="named-only"), allow_model_fallback=False),
    )
    assert not result.ok
    assert result.fallback_from is None
    assert len(runner.calls) == 1


async def test_no_fallback_for_a_non_model_failure() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=2, stderr="syntax error in prompt"))
    adapter = FakeAdapter("cursorish", fallback_model="auto")
    result = await _service([adapter], runner).delegate(_req(target=Target(cli="cursorish", model="named-only")))
    assert not result.ok
    assert result.fallback_from is None  # a real failure is not retried
    assert len(runner.calls) == 1
