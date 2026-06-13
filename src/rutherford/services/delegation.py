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
import contextlib
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..adapters.base import CLIAdapter
from ..adapters.registry import AdapterRegistry
from ..config.schema import RutherfordConfig
from ..domain.enums import ActivityEventKind, Effort, JobStatus, SafetyMode, is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.errors import DepthLimitError, RegistryError, RutherfordError
from ..domain.models import (
    ActivityEvent,
    DelegationRequest,
    DelegationResult,
    ErrorInfo,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    Provenance,
    RunRecord,
    Target,
    Topology,
)
from ..io.ledger import RunLedger
from ..runtime.cooldown import CooldownTracker
from ..runtime.depth import child_depth_env, child_lineage_env, current_lineage_count, ensure_within_depth
from ..runtime.failures import classify_failure, indicates_unhealthy, is_model_unavailable, is_retryable
from ..runtime.logging import log_event
from ..runtime.platform import PlatformInfo, detect_platform
from ..runtime.process import ProcessRunner
from .roles import RoleStore

#: The raw line-stream sink: stderr lines (``on_progress``) or stdout lines (``on_stdout``), as they
#: arrive from the subprocess. Forwarded straight to the runner; the poll view (``job.progress``) is fed
#: from it. Unchanged by N1 -- the structured stream below is a SEPARATE, parallel channel.
ProgressCallback = Callable[[str], None]

#: The structured live-activity sink (N1, item 3): lifecycle :class:`ActivityEvent`s a service emits as a
#: run progresses (a voice starting/finishing, a panel boundary, a budget cut). A sync tool maps each to an
#: MCP progress push; distinct from :data:`ProgressCallback` so the raw line stream and the structured
#: lifecycle stream never have to be the same shape. Best-effort: a raising sink never breaks the run.
ActivityCallback = Callable[[ActivityEvent], None]


def emit_activity(on_activity: ActivityCallback | None, event: ActivityEvent) -> None:
    """Deliver an :class:`ActivityEvent` to a sink if one is listening; swallow any error (N1, item 3).

    Transparency is a side-channel: a buggy or slow activity sink must never fail (or abort) the run it is
    only observing, so every emission goes through here and a raising sink is silently dropped.
    """
    if on_activity is None:
        return
    with contextlib.suppress(Exception):  # a transparency sink must never break the run it observes
        on_activity(event)


def panel_cancelled_event(tool: str, depth: int) -> ActivityEvent:
    """The terminal ``job_cancelled`` event for a panel cancelled after it started (N1, item 3, 3-K).

    Emitted (via :class:`PanelLifecycle`) when a panel is cancelled after ``panel_started`` so the activity
    stream always closes with one terminal event rather than being orphaned -- a cancel can land at ANY of
    the panel's awaits (a voice wait, the live-tee stop, an active-harvest follow-up, the closing synthesis,
    or the parent-record persist), so the guarantee is centralized rather than guarded await-by-await.
    """
    return ActivityEvent(
        kind=ActivityEventKind.JOB_CANCELLED, tool=tool, depth=depth, status="cut", message=f"{tool} panel cancelled"
    )


class PanelLifecycle:
    """Guarantees a panel's activity stream emits EXACTLY ONE terminal event (N1, item 3, decision 3-K).

    A panel emits ``panel_started`` once it is past its up-front guards, then -- after any number of awaits
    (voice waits, the live-tee stop, active harvest, the closing synthesis, the record persist) -- exactly
    one terminal: ``panel_finished`` on a clean finish or a budget-exhausted failure, or ``job_cancelled``
    if it is cancelled anywhere in between. Because a cancellation can surface at ANY of those awaits, the
    panel body is wrapped once (see the panel services) and the terminal is emitted here -- tracking
    ``started`` (so a cancel BEFORE the panel started emits nothing) and ``closed`` (so a terminal is never
    emitted twice).
    """

    def __init__(self, tool: str, depth: int, on_activity: ActivityCallback | None) -> None:
        self._tool = tool
        self._depth = depth
        self._on_activity = on_activity
        self._started = False
        self._closed = False

    def mark_started(self, event: ActivityEvent) -> None:
        """Emit the ``panel_started`` event and record that the panel is live."""
        self._started = True
        emit_activity(self._on_activity, event)

    def mark_closed(self, event: ActivityEvent) -> None:
        """Emit a terminal ``panel_finished`` event (clean or failed) and record the panel as closed."""
        self._closed = True
        emit_activity(self._on_activity, event)

    def on_cancel(self) -> None:
        """Emit the terminal ``job_cancelled`` -- but only if the panel started and has not already closed."""
        if self._started and not self._closed:
            self._closed = True
            emit_activity(self._on_activity, panel_cancelled_event(self._tool, self._depth))


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
        on_stdout: ProgressCallback | None = None,
        on_activity: ActivityCallback | None = None,
    ) -> DelegationResult:
        """Run ``req`` against its target CLI and return the normalized result.

        ``on_stdout`` (F8a, 2-F/2-G) receives the CLI's stdout lines as they stream, distinct from
        ``on_progress`` (stderr). A panel uses it to accumulate a per-voice partial answer that survives a
        time-budget cut; the jobs layer tees it into the run's artifacts. ``None`` disables streaming.

        ``on_activity`` (N1, item 3) receives structured :class:`ActivityEvent`s -- a ``voice_started`` once
        the run reaches execution and a single ``voice_finished`` with its outcome -- so a sync caller can be
        pushed live progress. A cross-target fallback runs its alternates silently (they do not re-emit), so
        a voice is exactly one started/finished pair regardless of how many alternates it tried.
        """
        created_at = self._clock()
        # Fill in the per-adapter configured default model when the call names none, so
        # ``[adapters.<id>] default_model`` is honored before the adapter ever sees the request.
        if req.target.model is None:
            configured = self._config.default_model_for(req.target.cli)
            if configured:
                req = req.model_copy(update={"target": req.target.model_copy(update={"model": configured})})
        # Resolve the reasoning-effort tier the same way (call value wins, else the configured default),
        # so an adapter's map_effort/build_invocation and the result stamp all see one resolved value (F8a).
        effort = req.effort if req.effort is not None else self._config.effort_for(req.target.cli)
        ctx = InvocationContext(
            target=req.target,
            safety_mode=req.safety_mode,
            working_dir=req.working_dir,
            correlation_id=correlation_id,
            session_id=req.session_id,
            extra_args=self._config.extra_args_for(req.target.cli),
            effort=effort,
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

        # N1: announce the voice as started when its subprocess actually LAUNCHES -- i.e. after the
        # concurrency semaphore is acquired inside _execute, not here. A voice that is still queued on the
        # semaphore when a budget deadline cuts it never launches, so it never emits a misleading "started".
        # The flag keeps it to one started even if a model fallback runs a second subprocess for this voice.
        started = False

        def on_launch() -> None:
            nonlocal started
            if started:
                return
            started = True
            emit_activity(
                on_activity,
                ActivityEvent(
                    kind=ActivityEventKind.VOICE_STARTED,
                    correlation_id=correlation_id,  # the stable per-voice key (survives a model fallback)
                    cli=req.target.cli,
                    model=req.target.model,
                    role=req.role,
                    depth=base_depth,
                    status="started",
                    message=f"{req.target.display_label} started",
                ),
            )

        result, raw, spec = await self._execute(adapter, req, ctx, base_depth, on_progress, on_stdout, on_launch)
        # N1 (decision 3-A): count every subprocess delegation this call launches, so realized fan-out
        # includes fallback re-runs (not just the one declared seat). The primary attempt is 1.
        attempts = 1

        fallback_model = self._model_fallback_for(adapter, req, result)
        if fallback_model is not None:
            result, raw, spec = await self._fallback(
                adapter, req, ctx, base_depth, on_progress, on_stdout, fallback_model
            )
            attempts += 1  # a model-fallback re-run is a second subprocess delegation

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
            recovered, alternate_attempts = await self._fallback_chain(
                req, result, correlation_id, base_depth, on_progress, on_stdout
            )
            # Every alternate the chain tried counts toward realized fan-out, win or lose (3-A). Folding it
            # into ``attempts`` also covers the EXHAUSTED chain: the fall-through below stamps the primary's
            # result with this total, so failed alternates are not dropped from the count.
            attempts += alternate_attempts
            if recovered is not None:
                recovered.delegation_call_count = attempts  # primary + model fallback + every alternate tried
                self._log_delegation(req, result, correlation_id, base_depth)
                emit_activity(on_activity, _voice_finished_event(recovered, req.role, base_depth, correlation_id))
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
        # F8a: stamp the requested effort and the tier the adapter actually applied (after its clamp), so
        # a budget that silently did nothing is never silent. Best-effort: a buggy map_effort must not
        # fail a delegation that produced an answer.
        result.effort = effort
        result.effort_applied = self._effort_applied(adapter, effort)
        # N1: carry the runner's observed local-descendant peak up onto the result, so a panel can roll it
        # into its topology. ``None`` when nothing ran (a build failure) or the runner did not sample.
        result.observed_peak_agents = raw.observed_peak_agents if raw is not None else None
        # N1 (3-A): record how many subprocess delegations this seat launched (primary + any model fallback)
        # so a panel's realized fan-out counts the fallback re-runs.
        result.delegation_call_count = attempts
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
        emit_activity(on_activity, _voice_finished_event(result, req.role, base_depth, correlation_id))
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
        on_stdout: ProgressCallback | None = None,
        on_launch: Callable[[], None] | None = None,
    ) -> tuple[DelegationResult, ProcessResult | None, InvocationSpec | None]:
        """Build, run, and parse one invocation. Returns the result, its raw process output, and the spec.

        Raw and spec are ``None`` only when ``build_invocation`` itself failed (nothing ran). The spec
        is returned so the delegation can pin its ``argv`` into a durable run record (F2). ``on_launch``
        (N1, item 3) fires once the concurrency semaphore is acquired and the subprocess is about to run,
        so a caller can mark the voice "launched" only when it truly launches (not while it is still queued).
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

        # Propagate the recursion-depth guard and the N1 lineage signal (count-first: the lineage count
        # incremented for the child, plus the panel parent run id when this is a panel voice) so a nested
        # Rutherford host reads where it sits in the agent tree and the aggregate cap can reason across layers.
        child_env = {
            **child_depth_env(base_depth),
            **child_lineage_env(parent_run_id=req.parent_run_id, current_count=current_lineage_count()),
        }
        spec = spec.model_copy(update={"env": {**spec.env, **child_env}})
        # Precedence: an explicit per-call timeout, else the per-adapter ``[adapters.<id>]
        # timeout_s``, else the global default. A slow local model can set the per-adapter one.
        timeout = req.timeout_s or self._config.timeout_for(req.target.cli) or self._config.default_timeout_s
        # Gate the actual subprocess on the global concurrency semaphore so a wide panel cannot launch
        # more than ``max_concurrency`` heavy agents at once. Held only around the run, not the
        # pure build/parse, to keep the critical section to the expensive part.
        try:
            async with self._semaphore:
                if on_launch is not None:  # N1: the voice has the slot and is launching now (not just queued)
                    on_launch()
                raw = await self._runner.run(spec, timeout, on_progress, on_stdout)
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
        on_stdout: ProgressCallback | None,
        fallback_model: str,
    ) -> tuple[DelegationResult, ProcessResult | None, InvocationSpec | None]:
        """Re-run the request once with ``fallback_model``, recording the fallback."""
        original_model = req.target.model
        fb_target = req.target.model_copy(update={"model": fallback_model})
        fb_req = req.model_copy(update={"target": fb_target, "allow_model_fallback": False})
        fb_ctx = ctx.model_copy(update={"target": fb_target})
        result, raw, spec = await self._execute(adapter, fb_req, fb_ctx, base_depth, on_progress, on_stdout)
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
        on_stdout: ProgressCallback | None = None,
    ) -> tuple[DelegationResult | None, int]:
        """Try each fallback target in order; return the first successful result and the alternates' attempts.

        The failed primary leads the recorded ``fallback_chain`` -- by its *effective* label (the
        post-model-fallback target that actually failed). A benched (cooled-down) alternate is skipped.
        Each alternate is delegated fresh with no further fallback (so the chain cannot recurse) and no
        carried ``session_id`` (a different CLI's resume token does not transfer), at the same depth (a
        sibling retry, not a deeper delegation). The list is capped at ``max_targets`` so a long chain
        cannot fan out unbounded. Returns ``(recovered_or_None, alternate_attempts)``: the first successful
        result (or ``None`` when every alternate failed, so the caller keeps the primary's refined failure)
        AND the total subprocess delegations every alternate tried (win or lose), so the caller can count the
        whole chain into realized fan-out -- including an EXHAUSTED chain (3-A: realized incl. fallback).
        """
        failed_labels = [primary.target.display_label]
        alternate_attempts = 0
        for index, target in enumerate(req.fallback[: self._config.max_targets]):
            if self._cooldown.is_benched(target.cli):
                continue
            fb_req = req.model_copy(update={"target": target, "fallback": [], "session_id": None})
            recovered = await self.delegate(
                fb_req,
                correlation_id=f"{correlation_id}:fb{index}",
                base_depth=base_depth,
                on_progress=on_progress,
                on_stdout=on_stdout,
            )
            alternate_attempts += recovered.delegation_call_count  # this alternate's own subprocess attempts
            if recovered.ok:
                recovered.fallback_chain = failed_labels
                return recovered, alternate_attempts
            failed_labels.append(target.display_label)
        return None, alternate_attempts

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
        """Whether this run should be kept as a durable job (F2); see ``RutherfordConfig.wants_persist``."""
        return self._config.wants_persist(req.persist)

    def _effort_applied(self, adapter: CLIAdapter, effort: Effort | None) -> Effort | None:
        """The effort tier the adapter will actually apply after its clamp (F8a), or ``None``.

        ``None`` when no effort was requested or the adapter has no knob (a no-op ``map_effort``).
        Best-effort: a buggy ``map_effort`` degrades to ``None`` rather than failing a produced answer.
        """
        if effort is None:
            return None
        try:
            return adapter.map_effort(effort).applied
        except Exception:
            return None

    def resolve_effort(self, cli: str, effort: Effort | None) -> Effort | None:
        """The effort a ``cli`` voice runs with: the call value, else the configured default (F8a, 2-L).

        Public so a panel (consensus/debate) reports the SAME resolved tier on a voice it cut at the budget
        -- whose subprocess was launched with this effort -- as the delegation hot path uses internally.
        """
        return effort if effort is not None else self._config.effort_for(cli)

    def applied_effort(self, cli: str, effort: Effort | None) -> Effort | None:
        """The tier ``cli``'s adapter actually applies for ``effort`` (F8a, 2-L-map), or ``None``.

        Public companion to :meth:`resolve_effort`, used by a panel to stamp ``effort_applied`` on a cut
        voice (no full delegation result of its own). Best-effort: an unknown cli or buggy hook -> ``None``.
        """
        if effort is None:
            return None
        try:
            return self._effort_applied(self._registry.get(cli), effort)
        except Exception:
            return None

    def recover_session(
        self, target: Target, partial_text: str, safety_mode: SafetyMode, effort: Effort | None
    ) -> str | None:
        """Recover a resumable session handle from a cut voice's streamed partial (F8a, 2-I), or ``None``.

        Parses the partial through the adapter's ``parse_output`` (``supports_partial_output`` only -- a
        single-envelope adapter streams no session before the end), returning the session the stream
        established even when no answer was produced yet, so a cut voice can be resumed later. Best-effort:
        an unknown cli, a non-partial adapter, an empty partial, or a raising parse all yield ``None``.
        """
        if not partial_text.strip():
            return None
        try:
            adapter = self._registry.get(target.cli)
            if not adapter.capabilities().supports_partial_output:
                return None
            ctx = InvocationContext(target=target, safety_mode=safety_mode, effort=effort)
            return adapter.parse_output(ProcessResult(exit_code=0, stdout=partial_text), ctx).session_id
        except Exception:
            return None

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
            # F8a: the effort requested for this run and the tier the adapter actually applied (post-clamp),
            # so a persisted record shows what the producer was asked to spend and what was enforced.
            requested_effort=result.effort,
            effort_applied=result.effort_applied,
            argv=list(spec.argv) if spec is not None else [],
            cwd=spec.cwd if spec is not None else req.working_dir,
            prompt=req.prompt,
            role=req.role,
            files=list(req.files),
            ok=result.ok,
            error_code=result.error.code if result.error is not None else None,
            changed_files=result.changed_files or [],
            cost=result.cost,
            # F8a: a leaf run harvested at a panel budget carries its stop_reason so the child record
            # agrees with the parent's harvest disposition. ``None`` on a clean finish.
            stop_reason=result.stop_reason,
            # N1 (item 3): a single delegation declares width 1; realized counts the subprocess delegations it
            # launched, INCLUDING a model fallback re-run (3-A). ``observed_peak_agents`` is the sampled peak.
            topology=Topology(
                declared=1,
                realized_delegations=result.delegation_call_count,
                observed_peak_agents=result.observed_peak_agents,
            ),
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


def _voice_finished_event(result: DelegationResult, role: str | None, depth: int, correlation_id: str) -> ActivityEvent:
    """Build the ``voice_finished`` :class:`ActivityEvent` for a finished delegation (N1, item 3).

    ``status`` distinguishes a clean ``ok`` from a ``cut`` (a time-budget harvest, ``stop_reason='budget'``)
    and a plain ``failed``, so the push side can colour the outcome without re-reading the result.
    ``correlation_id`` is the stable per-voice key so this terminal event collapses onto the same activity
    row as its ``voice_started`` even when a model fallback changed ``result.target.model``.
    """
    if result.ok:
        status = "ok"
    elif result.stop_reason == "budget":
        status = "cut"
    else:
        status = "failed"
    message = f"{result.target.display_label} {status}"
    if result.duration_s:
        message += f" ({result.duration_s:.1f}s)"
    return ActivityEvent(
        kind=ActivityEventKind.VOICE_FINISHED,
        correlation_id=correlation_id,
        cli=result.target.cli,
        model=result.target.model,
        role=role,
        status=status,
        elapsed_s=result.duration_s,
        observed_agents=result.observed_peak_agents,
        depth=depth,
        message=message,
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
