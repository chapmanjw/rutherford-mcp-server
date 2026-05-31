# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The delegation service: the foundational orchestration primitive.

Hands one request to one CLI and returns the normalized envelope. Depends only on the abstract
``AdapterRegistry`` and ``ProcessRunner`` (by injection), so it is fully testable with fakes. It
applies the cross-cutting guards -- depth, trusted workspace -- and treats every operational
failure (unknown target, missing binary, timeout, non-zero exit, parse failure) as a structured
``DelegationResult`` rather than an exception, so a consensus panel never aborts on one bad voice.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..adapters.registry import AdapterRegistry
from ..config.schema import RutherfordConfig
from ..domain.enums import is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.errors import DepthLimitError, RegistryError, RutherfordError
from ..domain.models import (
    DelegationRequest,
    DelegationResult,
    ErrorInfo,
    InvocationContext,
    ProcessResult,
)
from ..runtime.depth import child_depth_env, ensure_within_depth
from ..runtime.process import ProcessRunner
from .roles import RoleStore

ProgressCallback = Callable[[str], None]


class DelegationService:
    """Executes a single delegation end to end."""

    def __init__(
        self,
        registry: AdapterRegistry,
        runner: ProcessRunner,
        config: RutherfordConfig,
        roles: RoleStore,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._config = config
        self._roles = roles

    async def delegate(
        self,
        req: DelegationRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
    ) -> DelegationResult:
        """Run ``req`` against its target CLI and return the normalized result."""
        ctx = InvocationContext(
            target=req.target,
            safety_mode=req.safety_mode,
            working_dir=req.working_dir,
            correlation_id=correlation_id,
            depth=base_depth,
        )

        try:
            adapter = self._registry.get(req.target.cli)
        except RegistryError as exc:
            return self._error(ctx, exc)

        try:
            ensure_within_depth(base_depth, self._config.max_depth)
        except DepthLimitError as exc:
            return self._error(ctx, exc)

        detected = adapter.detect()
        if not detected.installed:
            return self._fail(
                ctx,
                ErrorCode.BINARY_NOT_FOUND,
                f"{req.target.cli} is not installed or not on PATH",
            )

        if is_mutating(req.safety_mode) and not self._workspace_trusted(req):
            return self._fail(
                ctx,
                ErrorCode.WORKSPACE_NOT_TRUSTED,
                f"{req.safety_mode.value} mode requires a trusted workspace; set trust_workspace=true "
                "or add the directory to trusted_workspaces in config",
            )

        if req.role:
            try:
                ctx = ctx.model_copy(update={"role_preamble": self._roles.get(req.role).preamble})
            except RutherfordError as exc:
                return self._error(ctx, exc)

        try:
            spec = adapter.build_invocation(req, ctx)
        except Exception as exc:  # an adapter bug becomes a structured result, not a server crash
            return self._fail(ctx, ErrorCode.INTERNAL, f"build_invocation failed: {exc}")

        spec = spec.model_copy(update={"env": {**spec.env, **child_depth_env(base_depth)}})

        timeout = req.timeout_s or self._config.default_timeout_s
        raw = await self._runner.run(spec, timeout, on_progress)

        try:
            result = adapter.parse_output(raw, ctx)
        except Exception as exc:  # a quirky parse must not crash the server
            result = self._fail(ctx, ErrorCode.PARSE_ERROR, f"parse_output failed: {exc}", raw=raw)

        result.raw = _combine_raw(raw) if req.include_raw else None
        return result

    def _workspace_trusted(self, req: DelegationRequest) -> bool:
        """Return whether a mutating delegation is permitted for ``req``'s working directory."""
        if req.trust_workspace:
            return True
        if not req.working_dir:
            return False
        try:
            target_dir = Path(req.working_dir).resolve()
        except OSError:
            return False
        for trusted in self._config.trusted_workspaces:
            try:
                root = Path(trusted).resolve()
            except OSError:
                continue
            if target_dir == root or target_dir.is_relative_to(root):
                return True
        return False

    def _error(self, ctx: InvocationContext, exc: RutherfordError) -> DelegationResult:
        """Build a failed result from a guard exception."""
        return DelegationResult(
            target=ctx.target,
            ok=False,
            error=ErrorInfo(code=exc.code, message=exc.message, details=exc.details),
            safety_mode=ctx.safety_mode,
        )

    def _fail(
        self,
        ctx: InvocationContext,
        code: ErrorCode,
        message: str,
        *,
        raw: ProcessResult | None = None,
    ) -> DelegationResult:
        """Build a failed result with an explicit code and message."""
        return DelegationResult(
            target=ctx.target,
            ok=False,
            exit_code=raw.exit_code if raw is not None else None,
            duration_s=raw.duration_s if raw is not None else 0.0,
            error=ErrorInfo(code=str(code), message=message),
            safety_mode=ctx.safety_mode,
        )


def _combine_raw(raw: ProcessResult) -> str:
    """Combine stdout and stderr into a single debug string."""
    if raw.stderr.strip():
        return f"{raw.stdout}\n--- stderr ---\n{raw.stderr}"
    return raw.stdout
