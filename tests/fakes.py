# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Test doubles: ``FakeProbe``, ``FakeProcessRunner``, and ``FakeAdapter``.

The interface-driven design exists so the entire core can be tested without spawning a real CLI.
These fakes are the seams: a fake command probe for adapter metadata, a fake process runner for
the delegation hot path, and a fake adapter for the services, registry, and depth-guard tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rutherford.domain.enums import AuthState, OutputMode, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import (
    AdapterCapabilities,
    AuthStatus,
    DelegationRequest,
    DelegationResult,
    DetectResult,
    ErrorInfo,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)


class FakeProbe:
    """A :class:`~rutherford.runtime.probe.CommandProbe` backed by canned data."""

    def __init__(
        self,
        which_map: dict[str, str] | None = None,
        run_fn: Callable[[list[str]], ProcessResult] | None = None,
        default_result: ProcessResult | None = None,
    ) -> None:
        self._which = which_map or {}
        self._run_fn = run_fn
        self._default = default_result or ProcessResult(exit_code=0, stdout="", stderr="")
        self.calls: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return self._which.get(name)

    def run(
        self,
        argv: list[str],
        *,
        timeout_s: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        self.calls.append(list(argv))
        if self._run_fn is not None:
            return self._run_fn(argv)
        return self._default


class FakeProcessRunner:
    """A :class:`~rutherford.runtime.process.ProcessRunner` that returns canned results.

    Records every ``(spec, timeout_s)`` it is asked to run, so a test can assert the argv,
    env (including ``RUTHERFORD_DEPTH``), cwd, and stdin an adapter produced. With ``results``
    set, successive calls return successive results (for consensus fan-out).
    """

    def __init__(
        self,
        result: ProcessResult | None = None,
        results: Sequence[ProcessResult] | None = None,
    ) -> None:
        self._result = result
        self._results = list(results) if results is not None else None
        self._index = 0
        self.calls: list[tuple[InvocationSpec, float]] = []
        self.progress: list[str] = []

    async def run(
        self,
        spec: InvocationSpec,
        timeout_s: float,
        on_progress: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        self.calls.append((spec, timeout_s))
        if on_progress is not None:
            on_progress("working")
            self.progress.append("working")
        if self._results is not None:
            result = self._results[self._index % len(self._results)]
            self._index += 1
            return result
        return self._result or ProcessResult(exit_code=0, stdout="ok", stderr="")


class FakeAdapter:
    """A configurable :class:`~rutherford.adapters.base.CLIAdapter` for service-level tests."""

    def __init__(
        self,
        adapter_id: str = "fake",
        *,
        models: Sequence[str] = ("m1", "m2"),
        installed: bool = True,
        auth_state: AuthState = AuthState.AUTHENTICATED,
        supports_resume: bool = True,
    ) -> None:
        self.id = adapter_id
        self.display_name = adapter_id.replace("_", " ").title()
        self._models = list(models)
        self._installed = installed
        self._auth_state = auth_state
        self._supports_resume = supports_resume

    def detect(self) -> DetectResult:
        if not self._installed:
            return DetectResult(installed=False)
        return DetectResult(installed=True, path=f"/usr/bin/{self.id}", version="1.0.0")

    def check_auth(self) -> AuthStatus:
        return AuthStatus(state=self._auth_state)

    def available_models(self) -> list[str]:
        return list(self._models)

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_resume=self._supports_resume,
            supports_model_selection=True,
            supports_working_dir=True,
            output_mode=OutputMode.TEXT,
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        argv = [self.id, "-p", req.prompt]
        if req.target.model:
            argv += ["--model", req.target.model]
        env = {"RUTHERFORD_DEPTH": str(ctx.depth + 1)}
        return InvocationSpec(argv=argv, env=env, cwd=req.working_dir)

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        return SafetyFlags(args=[f"--safety={mode.value}"], note=mode.value)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        ok = raw.exit_code == 0 and not raw.timed_out
        error = None
        if not ok:
            code = ErrorCode.TIMEOUT if raw.timed_out else ErrorCode.NONZERO_EXIT
            error = ErrorInfo(code=str(code), message=raw.stderr or "failed")
        return DelegationResult(
            target=ctx.target,
            ok=ok,
            exit_code=raw.exit_code,
            text=raw.stdout.strip(),
            duration_s=raw.duration_s,
            session_id="fake-session" if ok else None,
            error=error,
            safety_mode=ctx.safety_mode,
        )
