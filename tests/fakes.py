# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Test doubles: ``FakeProbe``, ``FakeProcessRunner``, and ``FakeAdapter``.

The interface-driven design exists so the entire core can be tested without spawning a real CLI.
These fakes are the seams: a fake command probe for adapter metadata, a fake process runner for
the delegation hot path, and a fake adapter for the services, registry, and depth-guard tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rutherford.adapters.base import CLIAdapter
from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.panels import PanelCache
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
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
    Provenance,
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
        #: The ``timeout_s`` each ``run`` was asked to use, so a test can assert a probe ceiling.
        self.timeouts: list[float] = []

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
        self.timeouts.append(timeout_s)
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
        run_fn: Callable[[InvocationSpec], ProcessResult] | None = None,
        *,
        cycle: bool = False,
    ) -> None:
        self._result = result
        self._results = list(results) if results is not None else None
        self._run_fn = run_fn
        self._cycle = cycle
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
        if self._run_fn is not None:
            # argv-aware: lets a test decide the outcome from the model/cli in the spec, which is
            # how the per-target model-fallback path is exercised.
            return self._run_fn(spec)
        if self._results is not None:
            # Exhausting the canned list is an ERROR unless cycling was explicitly requested: an
            # extra subprocess call (a fan-out or fallback regression) must fail the test loudly,
            # not silently re-serve an earlier outcome.
            if self._index >= len(self._results) and not self._cycle:
                raise AssertionError(
                    f"FakeProcessRunner exhausted its {len(self._results)} canned result(s) on call "
                    f"#{self._index + 1}; pass cycle=True if re-serving results is intended"
                )
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
        fallback_model: str | None = None,
        optional: bool = False,
        contract_ok: bool = True,
        provider: str | None = None,
    ) -> None:
        self.id = adapter_id
        self.display_name = adapter_id.replace("_", " ").title()
        self.optional = optional
        self._models = list(models)
        self._installed = installed
        self._auth_state = auth_state
        self._supports_resume = supports_resume
        self._fallback_model = fallback_model
        self._contract_ok = contract_ok
        self._provider = provider

    def detect(self) -> DetectResult:
        if not self._installed:
            return DetectResult(installed=False)
        return DetectResult(installed=True, path=f"/usr/bin/{self.id}", version="1.0.0")

    def check_auth(self) -> AuthStatus:
        return AuthStatus(state=self._auth_state)

    def available_models(self) -> list[str]:
        return list(self._models)

    def fallback_model(self) -> str | None:
        return self._fallback_model

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_resume=self._supports_resume,
            supports_model_selection=True,
            supports_working_dir=True,
            output_mode=OutputMode.TEXT,
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.id, "-p", prompt]
        if req.target.model:
            argv += ["--model", req.target.model]
        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        # Note: RUTHERFORD_DEPTH is overlaid by the delegation service, not by the adapter.
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    @staticmethod
    def _with_files(prompt: str, files: list[str]) -> str:
        if not files:
            return prompt
        listing = "\n".join(f"- {path}" for path in files)
        return f"{prompt}\n\nFiles in scope:\n{listing}"

    @staticmethod
    def _compose_prompt(prompt: str, preamble: str | None) -> str:
        return prompt if not preamble else f"{preamble}\n\n---\n\n{prompt}"

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        return SafetyFlags(args=[f"--safety={mode.value}"], note=mode.value)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        ok = raw.exit_code == 0 and not raw.timed_out
        error = None
        if not ok:
            code = ErrorCode.TIMEOUT if raw.timed_out else ErrorCode.NONZERO_EXIT
            error = ErrorInfo(code=code, message=raw.stderr or "failed")
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

    def check_output_contract(self, raw: ProcessResult) -> bool:
        return self._contract_ok

    def provenance(self, ctx: InvocationContext) -> Provenance:
        return Provenance(provider=self._provider, model=ctx.target.model, confirmed=self._provider is not None)


def make_app(
    *,
    adapters: Sequence[CLIAdapter] | None = None,
    runner: FakeProcessRunner | None = None,
    config: RutherfordConfig | None = None,
    panels: PanelCache | None = None,
    base_depth: int = 0,
) -> AppContext:
    """Build an :class:`AppContext` wired to fakes, with no disk or subprocess access.

    ``adapters`` accepts any :class:`CLIAdapter`, so a test can mix a real adapter (driven by a
    :class:`FakeProbe` / :class:`FakeProcessRunner`) in with the :class:`FakeAdapter` doubles.
    ``panels`` seeds a :class:`PanelCache` so a test can drive the ``panel=`` paths without disk.
    """
    registry = AdapterRegistry(list(adapters) if adapters is not None else [FakeAdapter()])
    return build_app_context(
        config=config or RutherfordConfig(),
        runner=runner or FakeProcessRunner(),
        registry=registry,
        panels=panels,
        base_depth=base_depth,
    )
