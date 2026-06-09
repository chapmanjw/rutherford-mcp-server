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

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..adapters.base import CLIAdapter
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
from ..runtime.logging import log_event
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
        #: Bounds how many CLI subprocesses run at once across every panel that shares this service
        #: (consensus fan-out, debate rounds, nested self-delegation), so panel width does not become
        #: unbounded host process pressure.
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    async def delegate(
        self,
        req: DelegationRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
    ) -> DelegationResult:
        """Run ``req`` against its target CLI and return the normalized result."""
        # Fill in the per-adapter configured default model when the call names none, so
        # ``[adapters.<id>] default_model`` is honored before the adapter ever sees the request.
        if req.target.model is None:
            configured = self._config.default_model_for(req.target.cli)
            if configured:
                req = req.model_copy(update={"target": req.target.model_copy(update={"model": configured})})
        ctx = InvocationContext(
            target=req.target,
            safety_mode=req.safety_mode,
            working_dir=req.working_dir,
            correlation_id=correlation_id,
            depth=base_depth,
            extra_args=self._config.extra_args_for(req.target.cli),
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

        # Optional read_only enforcement: fingerprint the git tree before the run so a non-mutating
        # delegation that nonetheless writes is caught (off by default; only for git working dirs).
        verify = self._config.verify_read_only and not is_mutating(req.safety_mode) and bool(req.working_dir)
        before = await asyncio.to_thread(_git_fingerprint, req.working_dir) if verify and req.working_dir else None

        result, raw = await self._execute(adapter, req, ctx, base_depth, on_progress)

        if self._should_fallback(adapter, req, result):
            result, raw = await self._fallback(adapter, req, ctx, base_depth, on_progress)

        # Check the tree only when the run actually succeeded. A run that already failed (timeout,
        # non-zero exit, parse/contract error) keeps its real error -- overwriting it with
        # READONLY_VIOLATED would hide why it failed, and a partial write from a crashed run is not
        # the invariant this guards. ``fallback_from`` is carried onto the violation so a fallback on
        # the offending run is not lost.
        if result.ok and before is not None and req.working_dir:
            after = await asyncio.to_thread(_git_fingerprint, req.working_dir)
            if after is not None and after != before:
                fallback_from = result.fallback_from
                result = self._fail(
                    ctx,
                    ErrorCode.READONLY_VIOLATED,
                    f"{req.safety_mode.value} delegation to {req.target.cli} modified the git working tree "
                    "under working_dir, which a non-mutating mode must not do",
                    raw=raw,
                )
                result.fallback_from = fallback_from

        result.raw = _combine_raw(raw) if (req.include_raw and raw is not None) else None
        log_event(
            "delegate",
            correlation_id=correlation_id,
            cli=req.target.cli,
            model=req.target.model,
            safety_mode=req.safety_mode.value,
            depth=base_depth,
            duration_s=round(result.duration_s, 3),
            ok=result.ok,
            error_code=result.error.code if result.error else None,
            fallback_from=result.fallback_from,
        )
        return result

    async def _execute(
        self,
        adapter: CLIAdapter,
        req: DelegationRequest,
        ctx: InvocationContext,
        base_depth: int,
        on_progress: ProgressCallback | None,
    ) -> tuple[DelegationResult, ProcessResult | None]:
        """Build, run, and parse one invocation. Returns the result and its raw process output.

        Raw is ``None`` only when ``build_invocation`` itself failed (nothing ran).
        """
        try:
            spec = adapter.build_invocation(req, ctx)
        except RutherfordError as exc:  # a validation error (e.g. no model) keeps its own code
            return self._error(ctx, exc), None
        except Exception as exc:  # an adapter bug becomes a structured result, not a server crash
            return self._fail(ctx, ErrorCode.INTERNAL, f"build_invocation failed: {exc}"), None

        spec = spec.model_copy(update={"env": {**spec.env, **child_depth_env(base_depth)}})
        # Precedence: an explicit per-call timeout, else the per-adapter ``[adapters.<id>]
        # timeout_s``, else the global default. A slow local model can set the per-adapter one.
        timeout = req.timeout_s or self._config.timeout_for(req.target.cli) or self._config.default_timeout_s
        # Gate the actual subprocess on the global concurrency semaphore so a wide panel cannot launch
        # more than ``max_concurrency`` heavy agents at once. Held only around the run, not the
        # pure build/parse, to keep the critical section to the expensive part.
        async with self._semaphore:
            raw = await self._runner.run(spec, timeout, on_progress)

        try:
            result = adapter.parse_output(raw, ctx)
        except Exception as exc:  # a quirky parse must not crash the server
            result = self._fail(ctx, ErrorCode.PARSE_ERROR, f"parse_output failed: {exc}", raw=raw)

        # Drift canary: a result that claims success must still match the adapter's expected output
        # shape. Enforced only on an ``ok`` result (a failure already carries its own code) so a
        # silently drifted-but-clean run fails loudly with CONTRACT_MISMATCH instead of being trusted.
        if result.ok and not _contract_ok(adapter, raw):
            result = self._fail(
                ctx,
                ErrorCode.CONTRACT_MISMATCH,
                f"{req.target.cli} reported success but its output did not match the expected shape "
                "for this adapter -- the CLI's machine-readable output format may have changed",
                raw=raw,
            )
        return result, raw

    def _should_fallback(self, adapter: CLIAdapter, req: DelegationRequest, result: DelegationResult) -> bool:
        """Whether to retry once with the adapter's fallback model.

        Only when the caller allowed it, the run failed on a model-availability error, and the
        adapter offers a fallback that differs from what was already requested.
        """
        if not req.allow_model_fallback or result.ok or result.error is None:
            return False
        if not _is_model_unavailable_error(result.error.message):
            return False
        fallback = adapter.fallback_model()
        return fallback is not None and fallback != req.target.model

    async def _fallback(
        self,
        adapter: CLIAdapter,
        req: DelegationRequest,
        ctx: InvocationContext,
        base_depth: int,
        on_progress: ProgressCallback | None,
    ) -> tuple[DelegationResult, ProcessResult | None]:
        """Re-run the request once with the adapter's fallback model, recording the fallback."""
        original_model = req.target.model
        fb_target = req.target.model_copy(update={"model": adapter.fallback_model()})
        fb_req = req.model_copy(update={"target": fb_target, "allow_model_fallback": False})
        fb_ctx = ctx.model_copy(update={"target": fb_target})
        result, raw = await self._execute(adapter, fb_req, fb_ctx, base_depth, on_progress)
        result.fallback_from = original_model or "(default)"
        return result, raw

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


def _contract_ok(adapter: CLIAdapter, raw: ProcessResult | None) -> bool:
    """Whether the adapter's output contract holds for ``raw``, defaulting to ``True`` on any doubt.

    A defensive wrapper around ``adapter.check_output_contract``: a missing raw result (the build
    failed, nothing ran) or a buggy contract check must never turn a real success into a spurious
    CONTRACT_MISMATCH, so only an explicit ``False`` is treated as a contract violation.
    """
    if raw is None:
        return True
    try:
        return adapter.check_output_contract(raw) is not False
    except Exception:
        return True


def _git_fingerprint(working_dir: str) -> str | None:
    """Return a content fingerprint of the tree under ``working_dir``, or ``None`` if it cannot be read.

    Combines ``git status --porcelain --ignored=matching`` with the unstaged and staged diffs, all
    scoped to the ``working_dir`` subtree (the ``-- .`` pathspec), so the snapshot reflects content,
    not just status codes:

    * Status codes alone miss a *further* edit to an already-modified tracked file (its porcelain line
      is unchanged); the diffs catch the content change.
    * ``--ignored=matching`` surfaces a write to a gitignored path (a scratch dir, ``.env``, build
      output) that plain status hides. A pre-existing ignored file cancels out across before/after, so
      only a *new* ignored write moves the fingerprint.
    * ``-- .`` scopes to the subtree, so an unrelated change elsewhere in a larger repo is not
      mis-attributed to this delegation.

    ``None`` means the directory is not a git repo or git is unavailable, so verification is skipped.
    A non-``None`` value that differs before and after a run signals the subtree was mutated. Remaining
    limits, documented on ``verify_read_only``: a write *outside* the repo is unobservable this way,
    and under concurrent fan-out on a shared tree a peer's write can still be attributed here.
    """
    parts: list[str] = []
    for args in (
        ["status", "--porcelain", "--ignored=matching", "--", "."],
        ["diff", "--", "."],
        ["diff", "--cached", "--", "."],
    ):
        section = _git_run(working_dir, args)
        if section is None:
            return None
        parts.append(section)
    return "\x1e".join(parts)


def _git_run(working_dir: str, args: list[str]) -> str | None:
    """Run a read-only ``git`` command in ``working_dir`` and return stdout, or ``None`` on failure.

    ``--no-optional-locks`` keeps a concurrent ``git`` process from being blocked by (or blocking on)
    the index lock during these read-only queries.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", working_dir, "--no-optional-locks", *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


#: Substrings that mark a failure as "this model is not available to you" rather than a real
#: error. Matched case-insensitively against the error message. Kept broad on purpose: the cost
#: of a false positive is one extra retry on the adapter's default model, which is cheap and safe.
_MODEL_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "named models unavailable",
    "switch to auto",
    "only use auto",
    "model is not available",
    "model not available",
    "model unavailable",
    "model_unavailable",
    "no access to model",
    "not available on your plan",
    "upgrade your plan",
    "upgrade plans to continue",
    "unknown model",
    "invalid model",
)


def _is_model_unavailable_error(message: str) -> bool:
    """Heuristic: does ``message`` look like a model-availability rejection (vs. a real failure)?"""
    lowered = message.lower()
    return any(marker in lowered for marker in _MODEL_UNAVAILABLE_MARKERS)
