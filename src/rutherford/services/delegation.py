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
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..adapters.base import CLIAdapter
from ..adapters.registry import AdapterRegistry
from ..config.schema import RutherfordConfig
from ..domain.enums import JobStatus, is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.errors import DepthLimitError, RegistryError, RutherfordError
from ..domain.models import (
    DelegationRequest,
    DelegationResult,
    ErrorInfo,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    Provenance,
    RunRecord,
)
from ..io.ledger import RunLedger
from ..runtime.cooldown import CooldownTracker
from ..runtime.depth import child_depth_env, ensure_within_depth
from ..runtime.failures import classify_failure, indicates_unhealthy, is_model_unavailable, is_retryable
from ..runtime.logging import log_event
from ..runtime.platform import PlatformInfo, detect_platform
from ..runtime.process import ProcessRunner
from .roles import RoleStore

ProgressCallback = Callable[[str], None]

#: Conservative preflight bound for an argv-borne prompt on Windows: ``CreateProcessW`` caps the
#: command line at 32767 chars, and ``prepare_argv`` may still wrap the argv in a shim, so refuse
#: a little early rather than surface the cap as an opaque spawn failure.
_WINDOWS_CMDLINE_LIMIT = 30000


class DelegationService:
    """Executes a single delegation end to end."""

    def __init__(
        self,
        registry: AdapterRegistry,
        runner: ProcessRunner,
        config: RutherfordConfig,
        roles: RoleStore,
        *,
        platform: PlatformInfo | None = None,
        ledger: RunLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._config = config
        self._roles = roles
        #: Resolved once at construction; injectable so the Windows command-line preflight is
        #: unit-testable from any host.
        self._platform = platform if platform is not None else detect_platform()
        #: The durable run ledger (F2). ``None`` disables persistence entirely (e.g. a test with no
        #: jobs dir); when set, a run opting into persistence is written under its root.
        self._ledger = ledger
        #: Wall-clock source for run-record timestamps, injectable so persistence is testable.
        self._clock = clock
        #: Bounds how many CLI subprocesses run at once across every panel that shares this service
        #: (consensus fan-out, debate rounds, nested self-delegation), so panel width does not become
        #: unbounded host process pressure.
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        #: Process-global cooldown state (F7): benches a flapping adapter so a panel/fallback stops
        #: reaching for it. Shared across every panel that uses this service.
        self._cooldown = CooldownTracker(
            threshold=config.cooldown_threshold,
            window_s=config.cooldown_window_s,
            duration_s=config.cooldown_duration_s,
        )

    def is_benched(self, cli: str) -> bool:
        """Whether ``cli`` is currently on cooldown (F7), so a panel/fallback should skip it."""
        return self._cooldown.is_benched(cli)

    def cooldown_remaining_s(self, cli: str) -> float:
        """Seconds until ``cli``'s cooldown lifts, for a human-readable skip reason."""
        return self._cooldown.remaining_s(cli)

    async def delegate(
        self,
        req: DelegationRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_progress: ProgressCallback | None = None,
    ) -> DelegationResult:
        """Run ``req`` against its target CLI and return the normalized result."""
        created_at = self._clock()
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
            session_id=req.session_id,
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

        try:
            # detect() shells out on a probe-cache miss, so run it off-thread to keep the loop free.
            detected = await asyncio.to_thread(adapter.detect)
        except Exception as exc:  # a buggy adapter probe becomes a structured failure, not a panel abort
            return self._fail(ctx, ErrorCode.INTERNAL, f"{req.target.cli} adapter detect() raised: {exc}")
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
        # Snapshot the changed files before a mutating persisted run so the record reports its delta.
        before_changed = await self._snapshot_changed_files(req)

        result, raw, spec = await self._execute(adapter, req, ctx, base_depth, on_progress)

        fallback_model = self._model_fallback_for(adapter, req, result)
        if fallback_model is not None:
            result, raw, spec = await self._fallback(adapter, req, ctx, base_depth, on_progress, fallback_model)

        # Refine a generic non-zero exit into a specific failure category (rate-limit / auth / context
        # overflow / model-unavailable) so a caller -- and the fallback decision below -- can act on it.
        self._refine_failure(result)
        # Feed the cooldown tracker: a success clears this adapter's streak, an unhealthy failure
        # counts toward benching it. Recorded for the primary here; a fallback target records its own
        # health inside its own delegation.
        self._record_health(req.target.cli, result)

        # Cross-target fallback (F7): if the primary failed on a retryable category and a chain was
        # given, try each alternate in turn. Restricted to non-mutating modes -- retrying a write/yolo
        # task on a second CLI against the same (possibly partially-mutated) working_dir would compound
        # two agents' edits; write-mode reliability waits for worktree isolation (F6). A winning
        # alternate is a fully-processed result (its own provenance, cooldown, logging) that we adopt,
        # logging the primary's failure first so the "why we fell back" is not lost.
        if (
            not result.ok
            and req.fallback
            and not is_mutating(req.safety_mode)
            and result.error is not None
            and is_retryable(result.error.code)
        ):
            recovered = await self._fallback_chain(req, result, correlation_id, base_depth, on_progress)
            if recovered is not None:
                self._log_delegation(req, result, correlation_id, base_depth)
                return recovered

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

        # F3: stamp who actually answered (provider/model/backend from the adapter, CLI version from
        # the detect() probe already run above -- no extra subprocess). Best-effort: a buggy provenance
        # hook must never fail the delegation, and an all-unknown result keeps provenance absent.
        result.provenance = self._provenance(adapter, ctx, result, detected.version)
        result.raw = _combine_raw(raw) if (req.include_raw and raw is not None) else None
        self._log_delegation(req, result, correlation_id, base_depth)
        # Off-thread: persistence runs blocking git subprocesses + file I/O, and delegate() is the
        # convergence point shared by concurrent panel voices -- keep the event loop free, matching
        # how _git_fingerprint and adapter.detect are already offloaded above. Gated on actually
        # persisting so an ephemeral run (the default) does not pay a thread-pool round-trip just to
        # early-return -- _maybe_persist is a no-op otherwise, and that needless hop on every
        # delegation also added latency that destabilized the async-job poll timing.
        if self._ledger is not None and self._should_persist(req):
            await asyncio.to_thread(
                self._maybe_persist, req, result, spec, detected.version, created_at, before_changed
            )
        return result

    def _log_delegation(
        self, req: DelegationRequest, result: DelegationResult, correlation_id: str, base_depth: int
    ) -> None:
        """Emit the structured ``delegate`` log line for a finished result (or a primary that a
        fallback recovered, so the reason for falling back is recorded)."""
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

    async def _execute(
        self,
        adapter: CLIAdapter,
        req: DelegationRequest,
        ctx: InvocationContext,
        base_depth: int,
        on_progress: ProgressCallback | None,
    ) -> tuple[DelegationResult, ProcessResult | None, InvocationSpec | None]:
        """Build, run, and parse one invocation. Returns the result, its raw process output, and the spec.

        Raw and spec are ``None`` only when ``build_invocation`` itself failed (nothing ran). The spec
        is returned so the delegation can pin its ``argv`` into a durable run record (F2).
        """
        try:
            spec = adapter.build_invocation(req, ctx)
        except RutherfordError as exc:  # a validation error (e.g. no model) keeps its own code
            return self._error(ctx, exc), None, None
        except Exception as exc:  # an adapter bug becomes a structured result, not a server crash
            return self._fail(ctx, ErrorCode.INTERNAL, f"build_invocation failed: {exc}"), None, None

        # Windows-only preflight: several adapters carry the composed prompt in argv, and a command
        # line past the ~32K CreateProcessW cap fails opaquely as SPAWN_FAILED -- an unhealthy code
        # that would wrongly bench the seat. Refuse up front as CONTEXT_OVERFLOW instead, which is
        # retryable (a fallback chain can recover) but never counts toward cooldown.
        if spec.stdin is None and self._platform.is_windows:
            cmdline_len = len(subprocess.list2cmdline(spec.argv))
            if cmdline_len > _WINDOWS_CMDLINE_LIMIT:
                return (
                    self._fail(
                        ctx,
                        ErrorCode.CONTEXT_OVERFLOW,
                        f"composed command line is {cmdline_len} chars, over the ~32K Windows command-line "
                        "cap; use a CLI that passes the prompt on stdin (claude_code, codex, cursor, "
                        "opencode, qwen) or shorten the prompt",
                    ),
                    None,
                    spec,
                )

        spec = spec.model_copy(update={"env": {**spec.env, **child_depth_env(base_depth)}})
        # Precedence: an explicit per-call timeout, else the per-adapter ``[adapters.<id>]
        # timeout_s``, else the global default. A slow local model can set the per-adapter one.
        timeout = req.timeout_s or self._config.timeout_for(req.target.cli) or self._config.default_timeout_s
        # Gate the actual subprocess on the global concurrency semaphore so a wide panel cannot launch
        # more than ``max_concurrency`` heavy agents at once. Held only around the run, not the
        # pure build/parse, to keep the critical section to the expensive part.
        try:
            async with self._semaphore:
                raw = await self._runner.run(spec, timeout, on_progress)
        except OSError as exc:  # the subprocess could not be launched (a broken shim, a runtime error)
            return self._fail(ctx, ErrorCode.SPAWN_FAILED, f"failed to launch {req.target.cli}: {exc}"), None, spec

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
        return result, raw, spec

    def _model_fallback_for(self, adapter: CLIAdapter, req: DelegationRequest, result: DelegationResult) -> str | None:
        """The adapter's fallback model to retry once with, or ``None`` when no retry should happen.

        Only when the caller allowed it, the run failed on a model-availability error, and the
        adapter offers a fallback that differs from what was already requested. The hook is guarded:
        a buggy ``fallback_model()`` must not abort a delegation that already holds a result.
        """
        if not req.allow_model_fallback or result.ok or result.error is None:
            return None
        if not is_model_unavailable(result.error.message):
            return None
        try:
            fallback = adapter.fallback_model()
        except Exception:
            return None
        return fallback if fallback is not None and fallback != req.target.model else None

    async def _fallback(
        self,
        adapter: CLIAdapter,
        req: DelegationRequest,
        ctx: InvocationContext,
        base_depth: int,
        on_progress: ProgressCallback | None,
        fallback_model: str,
    ) -> tuple[DelegationResult, ProcessResult | None, InvocationSpec | None]:
        """Re-run the request once with ``fallback_model``, recording the fallback."""
        original_model = req.target.model
        fb_target = req.target.model_copy(update={"model": fallback_model})
        fb_req = req.model_copy(update={"target": fb_target, "allow_model_fallback": False})
        fb_ctx = ctx.model_copy(update={"target": fb_target})
        result, raw, spec = await self._execute(adapter, fb_req, fb_ctx, base_depth, on_progress)
        result.fallback_from = original_model or "(default)"
        return result, raw, spec

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

    def _provenance(
        self,
        adapter: CLIAdapter,
        ctx: InvocationContext,
        result: DelegationResult,
        version: str | None,
    ) -> Provenance | None:
        """Resolve the effective provider/model/CLI-version that answered, or ``None`` when nothing is known.

        Asks the adapter for the semantic identity against the *effective* target (the fallback model
        when a fallback fired is already on ``result.target``), then fills in the CLI version the
        delegation already detected. Returns ``None`` when no field is known so the result carries no
        empty provenance block. A failing hook degrades to unknown rather than raising.
        """
        effective_ctx = ctx.model_copy(update={"target": result.target})
        try:
            prov = adapter.provenance(effective_ctx)
        except Exception:
            prov = Provenance()
        if version:
            prov = prov.model_copy(update={"cli_version": version})
        if prov.provider or prov.backend or prov.model or prov.cli_version:
            return prov
        return None

    async def _fallback_chain(
        self,
        req: DelegationRequest,
        primary: DelegationResult,
        correlation_id: str,
        base_depth: int,
        on_progress: ProgressCallback | None,
    ) -> DelegationResult | None:
        """Try each fallback target in order; return the first successful, fully-processed result.

        The failed primary leads the recorded ``fallback_chain`` -- by its *effective* label (the
        post-model-fallback target that actually failed). A benched (cooled-down) alternate is skipped.
        Each alternate is delegated fresh with no further fallback (so the chain cannot recurse) and no
        carried ``session_id`` (a different CLI's resume token does not transfer), at the same depth (a
        sibling retry, not a deeper delegation). The list is capped at ``max_targets`` so a long chain
        cannot fan out unbounded. Returns ``None`` when every alternate failed, so the caller keeps the
        primary's (refined) failure.
        """
        failed_labels = [primary.target.display_label]
        for index, target in enumerate(req.fallback[: self._config.max_targets]):
            if self._cooldown.is_benched(target.cli):
                continue
            fb_req = req.model_copy(update={"target": target, "fallback": [], "session_id": None})
            recovered = await self.delegate(
                fb_req,
                correlation_id=f"{correlation_id}:fb{index}",
                base_depth=base_depth,
                on_progress=on_progress,
            )
            if recovered.ok:
                recovered.fallback_chain = failed_labels
                return recovered
            failed_labels.append(target.display_label)
        return None

    def _record_health(self, cli: str, result: DelegationResult) -> None:
        """Feed the cooldown tracker from a finished result: success clears the streak, an unhealthy
        failure (down/throttled/mis-launching, not a bad prompt) counts toward benching ``cli``."""
        if result.ok:
            self._cooldown.record_success(cli)
        elif result.error is not None and indicates_unhealthy(result.error.code):
            self._cooldown.record_failure(cli)

    def _refine_failure(self, result: DelegationResult) -> None:
        """Refine a generic ``NONZERO_EXIT`` into a specific failure category, in place.

        Only the catch-all ``NONZERO_EXIT`` is refined -- a result that already carries a specific code
        (timeout, parse error, spawn failure, ...) keeps it. A message the classifier does not recognize
        is left as ``NONZERO_EXIT``.
        """
        if result.ok or result.error is None or result.error.code != ErrorCode.NONZERO_EXIT:
            return
        refined = classify_failure(result.error.message)
        if refined is not None:
            result.error.code = refined

    def _should_persist(self, req: DelegationRequest) -> bool:
        """Whether this run should be kept as a durable job (F2): explicit ``persist`` wins, else the
        configured ``default_persistence`` (Model A: ``ephemeral`` out of the box)."""
        return req.persist if req.persist is not None else (self._config.default_persistence == "job")

    async def _snapshot_changed_files(self, req: DelegationRequest) -> set[str] | None:
        """Off-thread before-snapshot of the working tree's changed files, for a mutating persisted run.

        Returns the set of files already dirty before execution so :meth:`_maybe_persist` can report
        only *this run's* delta and not pre-existing edits. ``None`` when not applicable (not persisting,
        not mutating, no working dir, no ledger, or not a git tree).
        """
        if self._ledger is None or not self._should_persist(req) or not is_mutating(req.safety_mode):
            return None
        if not req.working_dir:
            return None
        wd = _resolved_dir(req.working_dir)
        before = await asyncio.to_thread(_git_changed_files, wd, _exclude_pathspec(wd, self._ledger.root))
        return set(before) if before is not None else None

    def _maybe_persist(
        self,
        req: DelegationRequest,
        result: DelegationResult,
        spec: InvocationSpec | None,
        adapter_version: str | None,
        created_at: float,
        before_changed: set[str] | None = None,
    ) -> None:
        """Persist this run as a durable job (F2) when the call opts in -- best-effort, in place.

        Resolution: an explicit ``req.persist`` wins; ``None`` follows the configured
        ``default_persistence`` (Model A: ``ephemeral`` out of the box, so nothing is written unless
        asked). Nothing happens when persistence is off or no ledger is wired.

        Boundary: only a run that reached execution is recorded here. A run refused by an up-front guard
        (unknown target, depth, missing binary, untrusted workspace, role lookup) returns before this
        hook and is *not* persisted -- the corpus is post-launch outcomes (success and runtime failure),
        not pre-flight refusals. For a mutating run in a git tree the changed files (this run's delta vs
        the ``before_changed`` snapshot) and a HEAD diff are captured, with the jobs directory excluded
        so a run never reports Rutherford's own bookkeeping; a file already dirty before the run that the
        run merely edited further is not re-attributed (the honest limit of a status-level delta). A
        filesystem failure is swallowed -- a run that already produced an answer must never fail because
        its record could not be written -- leaving the result without ``run_dir``. ``req.parent_run_id``
        links a voice's record to its panel parent.
        """
        if not self._should_persist(req) or self._ledger is None:
            return

        diff: str | None = None
        if is_mutating(req.safety_mode) and req.working_dir:
            wd = _resolved_dir(req.working_dir)
            exclude = _exclude_pathspec(wd, self._ledger.root)
            after = _git_changed_files(wd, exclude)
            if after is not None:
                # Report this run's delta: files dirty after the run that were not already dirty before.
                result.changed_files = (
                    after if before_changed is None else [name for name in after if name not in before_changed]
                )
                diff = _git_diff(wd, exclude)

        record = RunRecord(
            run_id=uuid.uuid4().hex,
            kind="delegate",
            status=JobStatus.SUCCEEDED if result.ok else JobStatus.FAILED,
            created_at=created_at,
            finished_at=self._clock(),
            duration_s=result.duration_s,
            parent_run_id=req.parent_run_id,
            cli=req.target.cli,
            requested_model=req.target.model,  # pre-fallback; result.target.model is the resolved one
            model=result.target.model,
            adapter_version=adapter_version,
            provenance=result.provenance,
            safety_mode=req.safety_mode,
            argv=list(spec.argv) if spec is not None else [],
            cwd=spec.cwd if spec is not None else req.working_dir,
            prompt=req.prompt,
            role=req.role,
            files=list(req.files),
            ok=result.ok,
            error_code=result.error.code if result.error is not None else None,
            changed_files=result.changed_files or [],
            cost=result.cost,
        )
        try:
            run_dir = self._ledger.write(record, answer=result.text, diff=diff)
        except Exception as exc:  # persistence is best-effort; never fail a produced answer over a bad write
            log_event("run_persist_failed", run_id=record.run_id, error_type=type(exc).__name__, error=str(exc))
            return
        result.run_dir = str(run_dir)

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
            error=ErrorInfo(code=code, message=message),
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


def _resolved_dir(working_dir: str) -> str:
    """Canonicalize ``working_dir`` (resolve ``..``/symlinks), or return it unchanged on failure.

    Used so the exclude-pathspec offset and git's ``-C`` directory share one canonicalization.
    """
    try:
        return str(Path(working_dir).resolve())
    except OSError:
        return working_dir


def _exclude_pathspec(working_dir: str, jobs_root: Path) -> list[str]:
    """Return a git pathspec excluding the jobs directory, when it sits under ``working_dir`` (F2).

    A persisted run writes its artifacts under the jobs root; if that root is inside the run's working
    tree (the default ``<cwd>/.rutherford/jobs``), an unscoped ``git status`` would then report a
    *previous* job's files as changed by *this* run. Excluding that subtree keeps ``changed_files`` and
    the diff about the user's code. Empty when the jobs root is outside the working tree.
    """
    try:
        wd = Path(working_dir).resolve()
        root = jobs_root.resolve()
    except OSError:
        return []
    if wd in root.parents:  # the jobs root sits strictly under the working tree
        return [f":(exclude){root.relative_to(wd).as_posix()}"]
    return []


def _git_changed_files(working_dir: str, exclude: list[str] | None = None) -> list[str] | None:
    """Return the paths git reports changed under ``working_dir`` (best-effort), or ``None`` (F2).

    Parses ``git status --porcelain`` scoped to the subtree (with any ``exclude`` pathspecs appended,
    e.g. the jobs dir): each line is ``XY <path>`` (two status columns, a space, then the path), or
    ``XY <old> -> <new>`` for a rename, where the new path is kept. Quoting of paths with special
    characters is stripped. ``None`` means the directory is not a git repo or git is unavailable, so
    the run records *no* changed-file list rather than a false empty one (distinct from a clean tree,
    which records ``[]``).
    """
    out = _git_run(working_dir, ["status", "--porcelain", "--", ".", *(exclude or [])])
    if out is None:
        return None
    files: list[str] = []
    for line in out.splitlines():
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            files.append(path)
    return files


#: A created file larger than this is noted but not embedded in diff.md, to keep the artifact sane.
_MAX_NEW_FILE_DIFF_BYTES = 200_000


def _git_diff(working_dir: str, exclude: list[str] | None = None) -> str | None:
    """A write run's diff: tracked changes (``git diff HEAD``) PLUS the full contents of created files (1-E).

    ``git diff HEAD`` shows only tracked files, so a run that *creates* a file -- the common codegen
    case -- would be listed in ``changed_files`` (from ``git status``) yet absent from the diff. Each
    untracked, non-ignored file (``git ls-files --others --exclude-standard`` -- the same set ``status``
    reports as ``??``) is appended as a synthetic ``new file`` section so ``diff.md`` reflects what the
    run actually wrote. ``None`` when git is unavailable or the dir is not a repo (matching the rest of
    the git machinery); an all-empty result collapses to ``None`` so no empty ``diff.md`` is written.
    """
    exclude = exclude or []
    tracked = _git_run(working_dir, ["diff", "HEAD", "--", ".", *exclude])
    if tracked is None:
        return None
    sections: list[str] = [tracked] if tracked.strip() else []
    others = _git_run(working_dir, ["ls-files", "--others", "--exclude-standard", "--", ".", *exclude])
    for line in (others or "").splitlines():
        rel = line.strip().strip('"')
        if rel:
            sections.append(_render_new_file_diff(Path(working_dir) / rel, rel))
    combined = "\n".join(sections)
    return combined or None


def _render_new_file_diff(path: Path, rel: str) -> str:
    """Render a created (untracked) file as a synthetic ``new file`` diff section for ``diff.md``.

    Reads bytes first so a binary or oversize file is noted rather than dumped; a text file is emitted
    as added (``+``-prefixed) lines so the section reads as a real new-file diff.
    """
    header = f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n"
    try:
        raw = path.read_bytes()
    except OSError:
        return f"{header}(unreadable)\n"
    if len(raw) > _MAX_NEW_FILE_DIFF_BYTES:
        return f"{header}(new file omitted: {len(raw)} bytes)\n"
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"{header}(binary file: {len(raw)} bytes)\n"
    return header + "".join(f"+{line}\n" for line in content.splitlines())


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
