# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation service, driven entirely by fakes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rutherford.adapters.base import CLIAdapter
from rutherford.adapters.ollama import OllamaAdapter
from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import AdapterConfig, RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import DelegationRequest, InvocationContext, InvocationSpec, ProcessResult, Target
from rutherford.runtime.depth import ENV_DEPTH
from rutherford.runtime.platform import OSFamily, PlatformInfo
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProbe, FakeProcessRunner


def _service(
    adapters: Sequence[CLIAdapter],
    runner: FakeProcessRunner,
    config: RutherfordConfig | None = None,
    platform: PlatformInfo | None = None,
) -> DelegationService:
    return DelegationService(
        AdapterRegistry(list(adapters)),
        runner,
        config or RutherfordConfig(),
        load_roles(),
        platform=platform,
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


async def test_ollama_no_model_uses_configured_default_end_to_end() -> None:
    # The headline behavior, exercised through the real OllamaAdapter and the service together: a
    # no-model call resolves `[adapters.ollama] default_model`, and the adapter builds the right argv
    # (model + --hidethinking) with the prompt on stdin.
    adapter = OllamaAdapter(probe=FakeProbe(which_map={"ollama": "/usr/bin/ollama"}))
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="PONG\n"))
    config = RutherfordConfig(adapters={"ollama": AdapterConfig(default_model="m9")})
    result = await _service([adapter], runner, config).delegate(_req("ollama"))
    assert result.ok and result.text == "PONG"
    spec, _ = runner.calls[0]
    assert spec.argv == ["ollama", "run", "m9", "--hidethinking"]
    assert spec.stdin == "question"


async def test_ollama_configured_extra_args_reach_the_invocation() -> None:
    # `[adapters.ollama] extra_args` are resolved by the service into the context and appended.
    adapter = OllamaAdapter(probe=FakeProbe(which_map={"ollama": "/usr/bin/ollama"}))
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(adapters={"ollama": AdapterConfig(default_model="m9", extra_args=["--keepalive", "30s"])})
    await _service([adapter], runner, config).delegate(_req("ollama"))
    spec, _ = runner.calls[0]
    assert spec.argv == ["ollama", "run", "m9", "--hidethinking", "--keepalive", "30s"]


async def test_per_adapter_timeout_overrides_the_global_default() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(default_timeout_s=300.0, adapters={"fake": AdapterConfig(timeout_s=900.0)})
    await _service([FakeAdapter("fake")], runner, config).delegate(_req())
    _spec, timeout = runner.calls[0]
    assert timeout == 900.0


async def test_explicit_call_timeout_beats_the_per_adapter_one() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(adapters={"fake": AdapterConfig(timeout_s=900.0)})
    await _service([FakeAdapter("fake")], runner, config).delegate(_req(timeout_s=42.0))
    _spec, timeout = runner.calls[0]
    assert timeout == 42.0


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


async def test_a_raising_detect_probe_is_a_structured_internal_failure() -> None:
    # detect() runs before the guarded build/run/parse pipeline; a buggy adapter probe must
    # degrade to a structured failure (so a panel folds it as one bad voice), never raise.
    class _DetectRaises(FakeAdapter):
        def detect(self):
            raise RuntimeError("probe exploded")

    runner = FakeProcessRunner()
    result = await _service([_DetectRaises("fake")], runner).delegate(_req())
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "INTERNAL"
    assert "probe exploded" in result.error.message
    assert runner.calls == []  # nothing spawned


async def test_a_raising_fallback_model_hook_keeps_the_primary_failure() -> None:
    # The model-fallback hook is best-effort: an adapter whose fallback_model() raises must not
    # abort a delegation that already holds a (failed) result -- the primary failure is kept.
    class _FallbackRaises(FakeAdapter):
        def fallback_model(self) -> str | None:
            raise RuntimeError("hook exploded")

    runner = FakeProcessRunner(
        ProcessResult(exit_code=1, stderr="Named models unavailable on your plan. Switch to Auto.")
    )
    result = await _service([_FallbackRaises("fake")], runner).delegate(
        _req(target=Target(cli="fake", model="named-only"))
    )
    assert not result.ok
    assert result.fallback_from is None  # no fallback was attempted
    assert len(runner.calls) == 1


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


async def test_session_id_reaches_the_invocation_context() -> None:
    # End-to-end thread-through: a request's session_id lands on the InvocationContext the adapter is
    # built against, so a resume token actually reaches the adapter. This is what lets the antigravity
    # adapter resolve the right brain/ conversation on a resumed run instead of re-guessing the newest.
    seen: list[str | None] = []

    class _CapturingAdapter(FakeAdapter):
        def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
            seen.append(ctx.session_id)
            return super().build_invocation(req, ctx)

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    await _service([_CapturingAdapter("fake")], runner).delegate(_req(session_id="resume-xyz"))
    assert seen == ["resume-xyz"]


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


# --- F7: failure refinement, cross-target fallback, cooldown -----------------


async def test_nonzero_exit_is_refined_to_a_specific_category() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=1, stderr="Error: 429 too many requests"))
    result = await _service([FakeAdapter("a")], runner).delegate(_req("a"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "RATE_LIMITED"  # refined from the generic NONZERO_EXIT


async def test_spawn_failure_is_structured_not_an_exception() -> None:
    def run_fn(spec: InvocationSpec) -> ProcessResult:
        raise OSError("exec format error")

    runner = FakeProcessRunner(run_fn=run_fn)
    result = await _service([FakeAdapter("a")], runner).delegate(_req("a"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "SPAWN_FAILED"


async def test_cross_target_fallback_recovers_from_a_retryable_primary_failure() -> None:
    def run_fn(spec: InvocationSpec) -> ProcessResult:
        cli = spec.argv[0]
        if cli == "a":
            return ProcessResult(exit_code=1, stderr="rate limit exceeded (429)")
        return ProcessResult(exit_code=0, stdout="answer from b")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _service([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.delegate(_req("a", fallback=[Target(cli="b")]))
    assert result.ok
    assert result.text == "answer from b"
    assert result.target.cli == "b"  # the alternate that answered
    assert result.fallback_chain == ["a"]  # the primary that failed first


async def test_fallback_chain_exhausted_keeps_the_primary_failure() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=1, stderr="boom"))  # everyone fails
    service = _service([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.delegate(_req("a", fallback=[Target(cli="b")]))
    assert not result.ok
    assert result.target.cli == "a"  # the primary's failure is preserved
    assert result.fallback_chain is None


async def test_no_fallback_chain_when_failure_is_not_retryable() -> None:
    # WORKSPACE_NOT_TRUSTED (a write to an untrusted dir) is terminal: a different CLI would fail the
    # same way, so the chain is not attempted.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _service([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.delegate(
        _req("a", safety_mode=SafetyMode.WRITE, fallback=[Target(cli="b")]),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "WORKSPACE_NOT_TRUSTED"
    assert result.fallback_chain is None
    assert len(runner.calls) == 0  # nothing ran (the guard fired before any subprocess)


async def test_unhealthy_failures_bench_an_adapter_after_threshold() -> None:
    config = RutherfordConfig(cooldown_threshold=2, cooldown_window_s=100.0, cooldown_duration_s=100.0)
    runner = FakeProcessRunner(ProcessResult(exit_code=1, stderr="rate limit exceeded"))  # RATE_LIMITED
    service = _service([FakeAdapter("a")], runner, config)
    await service.delegate(_req("a"))
    assert not service.is_benched("a")  # one failure, below the threshold
    await service.delegate(_req("a"))
    assert service.is_benched("a")  # the second benches it


async def test_a_plain_nonzero_exit_does_not_bench_a_healthy_adapter() -> None:
    # A non-zero exit from a hard task is not an adapter-health signal, so it never benches -- even
    # repeatedly. Only typed seat failures (rate-limit, auth, timeout, spawn, drift) count.
    config = RutherfordConfig(cooldown_threshold=2, cooldown_window_s=100.0)
    runner = FakeProcessRunner(ProcessResult(exit_code=1, stderr="the task could not be completed"))
    service = _service([FakeAdapter("a")], runner, config)
    for _ in range(5):
        await service.delegate(_req("a"))
    assert not service.is_benched("a")


async def test_a_benched_fallback_target_is_skipped() -> None:
    config = RutherfordConfig(cooldown_threshold=1)  # one unhealthy failure benches
    calls: list[str] = []

    def run_fn(spec: InvocationSpec) -> ProcessResult:
        cli = spec.argv[0]
        calls.append(cli)
        if cli == "b":
            return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # unhealthy -> benches b
        if cli == "a":
            return ProcessResult(exit_code=1, stderr="the task failed")  # retryable, triggers fallback
        return ProcessResult(exit_code=0, stdout="answer from c")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _service([FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c")], runner, config)
    await service.delegate(_req("b"))  # bench b
    assert service.is_benched("b")
    calls.clear()
    result = await service.delegate(_req("a", fallback=[Target(cli="b"), Target(cli="c")]))
    assert result.ok
    assert result.target.cli == "c"
    assert "b" not in calls  # b was skipped (benched), never invoked
    assert result.fallback_chain == ["a"]  # only the primary actually failed in the chain


async def test_a_successful_delegation_clears_the_cooldown_streak() -> None:
    config = RutherfordConfig(cooldown_threshold=2, cooldown_window_s=100.0)
    state = {"fail": True}

    def run_fn(spec: InvocationSpec) -> ProcessResult:
        if state["fail"]:
            return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # unhealthy
        return ProcessResult(exit_code=0, stdout="ok")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _service([FakeAdapter("a")], runner, config)
    await service.delegate(_req("a"))  # 1 failure
    state["fail"] = False
    await service.delegate(_req("a"))  # success clears the streak
    state["fail"] = True
    await service.delegate(_req("a"))  # 1 failure again
    assert not service.is_benched("a")  # never reached two consecutive failures


async def test_no_cross_target_fallback_in_write_mode() -> None:
    # Fallback is restricted to non-mutating modes: re-running a write task on a second CLI against the
    # same (possibly partially-mutated) tree would compound edits. The primary's failure is kept.
    calls: list[str] = []

    def run_fn(spec: InvocationSpec) -> ProcessResult:
        calls.append(spec.argv[0])
        return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # retryable on a

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _service([FakeAdapter("a"), FakeAdapter("b")], runner)
    result = await service.delegate(
        _req("a", safety_mode=SafetyMode.WRITE, trust_workspace=True, fallback=[Target(cli="b")]),
    )
    assert not result.ok
    assert result.target.cli == "a"  # the primary's failure is kept
    assert result.fallback_chain is None
    assert "b" not in calls  # the alternate was never tried in write mode


async def test_contract_mismatch_benches_the_adapter() -> None:
    # Output drift (CONTRACT_MISMATCH) is an adapter-integration problem, so it counts toward cooldown
    # end to end: the contract canary fails on an otherwise-ok run, and the adapter is benched.
    config = RutherfordConfig(cooldown_threshold=1)
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _service([FakeAdapter("a", contract_ok=False)], runner, config)
    result = await service.delegate(_req("a"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "CONTRACT_MISMATCH"
    assert service.is_benched("a")


async def test_fallback_chain_records_the_effective_post_model_fallback_target() -> None:
    # cursorish:named-only model-falls-back to cursorish:auto (still fails), then b answers. The chain
    # records the EFFECTIVE failed label (cursorish:auto), not the originally requested named-only.
    def run_fn(spec: InvocationSpec) -> ProcessResult:
        if spec.argv[0] == "cursorish":
            if "named-only" in spec.argv:
                return ProcessResult(exit_code=1, stderr="Named models unavailable. Switch to Auto.")
            return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # the auto attempt also fails
        return ProcessResult(exit_code=0, stdout="answer from b")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _service([FakeAdapter("cursorish", fallback_model="auto"), FakeAdapter("b")], runner)
    result = await service.delegate(
        _req(target=Target(cli="cursorish", model="named-only"), fallback=[Target(cli="b")]),
    )
    assert result.ok
    assert result.target.cli == "b"
    assert result.fallback_chain == ["cursorish:auto"]  # the effective failed target, post model-fallback


async def test_fallback_chain_is_capped_at_max_targets() -> None:
    # The documented cap: a fallback list longer than max_targets is sliced, so a long chain cannot
    # fan out unbounded. With max_targets=2, only the first two alternates run; d is never tried.
    calls: list[str] = []

    def run_fn(spec: InvocationSpec) -> ProcessResult:
        calls.append(spec.argv[0])
        return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # retryable everywhere

    runner = FakeProcessRunner(run_fn=run_fn)
    config = RutherfordConfig(max_targets=2, cooldown_threshold=10)  # threshold high so nothing benches
    adapters = [FakeAdapter("a"), FakeAdapter("b"), FakeAdapter("c"), FakeAdapter("d")]
    result = await _service(adapters, runner, config).delegate(
        _req("a", fallback=[Target(cli="b"), Target(cli="c"), Target(cli="d")]),
    )
    assert not result.ok
    assert calls == ["a", "b", "c"]  # the primary plus at most max_targets alternates


async def test_fallback_alternate_does_not_inherit_the_primary_session_id() -> None:
    # A different CLI's resume token does not transfer: the alternate must be built with
    # session_id=None, not the primary's token.
    seen: dict[str, str | None] = {}

    class _CapturingAdapter(FakeAdapter):
        def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
            seen[self.id] = ctx.session_id
            return super().build_invocation(req, ctx)

    def run_fn(spec: InvocationSpec) -> ProcessResult:
        if spec.argv[0] == "a":
            return ProcessResult(exit_code=1, stderr="rate limit exceeded")  # retryable, triggers fallback
        return ProcessResult(exit_code=0, stdout="answer from b")

    runner = FakeProcessRunner(run_fn=run_fn)
    service = _service([_CapturingAdapter("a"), _CapturingAdapter("b")], runner)
    result = await service.delegate(_req("a", session_id="resume-abc", fallback=[Target(cli="b")]))
    assert result.ok
    assert seen["a"] == "resume-abc"  # the primary carried the caller's token
    assert seen["b"] is None  # the alternate saw it stripped


# --- Windows command-line length preflight --------------------------------------------------------


async def test_windows_oversized_argv_prompt_is_refused_as_context_overflow() -> None:
    # On Windows an argv-borne prompt past the ~32K CreateProcessW cap is refused up front as
    # CONTEXT_OVERFLOW (retryable, not unhealthy) instead of surfacing as an opaque SPAWN_FAILED
    # that would wrongly bench the seat. FakeAdapter puts the prompt in argv with stdin=None.
    runner = FakeProcessRunner()
    windows = PlatformInfo(os_family=OSFamily.WINDOWS, is_wsl=False)
    service = _service([FakeAdapter("fake")], runner, platform=windows)
    result = await service.delegate(_req(prompt="x" * 31_000))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "CONTEXT_OVERFLOW"
    assert "32K" in result.error.message and "stdin" in result.error.message
    assert runner.calls == []  # refused before any subprocess
    assert not service.is_benched("fake")  # the seat is not benched for a prompt-sized problem


async def test_oversized_argv_prompt_passes_through_off_windows() -> None:
    # The same spec on a non-Windows platform has no command-line cap and runs normally.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    linux = PlatformInfo(os_family=OSFamily.LINUX, is_wsl=False)
    result = await _service([FakeAdapter("fake")], runner, platform=linux).delegate(_req(prompt="x" * 31_000))
    assert result.ok
    assert len(runner.calls) == 1
