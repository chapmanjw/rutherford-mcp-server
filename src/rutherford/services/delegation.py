# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The delegation service: hand one request to one ACP agent and return the normalized envelope.

The ACP-native foundational primitive every tool bottoms out in. It resolves the agent descriptor, builds
the :class:`~rutherford.acp.permission.PermissionPolicy` from the safety mode (guarding the mutating modes
behind a trusted-workspace check), enforces the cross-cutting guards (the recursion-depth cap and the global
concurrency semaphore), composes the prompt with any in-scope files, and drives one ACP turn via
:func:`~rutherford.acp.session.run_acp_turn`. Every operational failure is returned as a structured
:class:`DelegationResult`, never raised, so a consensus panel never aborts on one bad voice.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import ActivityEventKind, Effort, is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ActivityEvent, DelegationRequest, DelegationResult, ErrorInfo, Target
from ..runtime.depth import ensure_within_depth

#: The structured live-activity sink (N1, item 3): lifecycle :class:`ActivityEvent`s a service emits as a run
#: progresses (a voice starting/finishing, a panel boundary, a budget cut). A sync tool maps each to an MCP
#: progress push; a background job buffers them for the ``activity`` poll table. Best-effort: a raising sink
#: never breaks the run it only observes.
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
    stream always closes with one terminal event rather than being orphaned -- a cancel can land at any of the
    panel's awaits, so the guarantee is centralized rather than guarded await-by-await.
    """
    return ActivityEvent(
        kind=ActivityEventKind.JOB_CANCELLED, tool=tool, depth=depth, status="cut", message=f"{tool} panel cancelled"
    )


class PanelLifecycle:
    """Guarantees a panel's activity stream emits EXACTLY ONE terminal event (N1, item 3, decision 3-K).

    A panel emits ``panel_started`` once it is past its up-front guards, then -- after any number of awaits
    (voice waits, the closing synthesis, the persist) -- exactly one terminal: ``panel_finished`` on a clean
    finish or a budget-exhausted failure, or ``job_cancelled`` if it is cancelled anywhere in between. Because
    a cancellation can surface at any of those awaits, the panel body is wrapped once (see the panel services)
    and the terminal is emitted here -- tracking ``started`` (so a cancel BEFORE the panel started emits
    nothing) and ``closed`` (so a terminal is never emitted twice).
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


class DelegationService:
    """Executes a single ACP delegation end to end."""

    def __init__(self, descriptors: DescriptorRegistry, config: RutherfordConfig) -> None:
        self._descriptors = descriptors
        self._config = config
        #: Bounds how many ACP agent sessions run at once across every panel that shares this service
        #: (the consensus fan-out, a debate's rounds, a nested self-delegation), so panel width does not
        #: become unbounded host process pressure (N1 / reliability). Held only around the ACP turn.
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """The shared concurrency semaphore, so a panel's budgeted/direct-session paths gate the SAME way.

        The consensus budget harvest and a debate round drive :class:`~rutherford.acp.session.ACPSession`
        turns directly (not through :meth:`delegate`), so they acquire this around each turn to keep one
        global ``max_concurrency`` ceiling across every path that spawns an agent.
        """
        return self._semaphore

    async def delegate(
        self,
        req: DelegationRequest,
        *,
        correlation_id: str = "",
        base_depth: int = 0,
        on_activity: ActivityCallback | None = None,
    ) -> DelegationResult:
        """Run ``req`` against its target agent and return the normalized result.

        ``base_depth`` (N1, item 3) is how deep this delegation sits in a Rutherford-driving-Rutherford chain:
        it is checked against ``max_depth`` (refused with ``MAX_DEPTH_EXCEEDED`` at the ceiling) and layered
        onto the spawned agent's environment so a nested host stays bounded. ``on_activity`` receives the
        ``voice_started`` (once the concurrency slot is acquired and the turn launches) and ``voice_finished``
        events so a sync caller is pushed live progress and a job buffers the per-voice table. The result's
        ``delegation_call_count`` is the subprocess delegations launched (1 today; a fallback re-run would add
        to it once cross-target fallback lands).
        """
        if not self._descriptors.has(req.target.cli):
            known = ", ".join(self._descriptors.ids()) or "(none)"
            return _fail(req, ErrorCode.UNKNOWN_TARGET, f"unknown agent id {req.target.cli!r}; known agents: {known}")
        descriptor = self._descriptors.get(req.target.cli)

        try:
            ensure_within_depth(base_depth, self._config.max_depth)
        except RutherfordError as exc:
            return _fail(req, exc.code, exc.message, details=exc.details)

        if is_mutating(req.safety_mode) and not self._workspace_trusted(req):
            return _fail(
                req,
                ErrorCode.WORKSPACE_NOT_TRUSTED,
                f"{req.safety_mode.value} mode requires a trusted workspace; set trust_workspace=true "
                "or add the directory to trusted_workspaces in config",
            )

        cwd = req.working_dir or str(Path.cwd())
        timeout = req.timeout_s or self._config.timeout_for(req.target.cli) or self._config.default_timeout_s
        policy = PermissionPolicy(mode=req.safety_mode)
        prompt = _compose_prompt(req.prompt, req.files)

        started = False

        def on_launch() -> None:
            # N1: announce the voice as started only once its concurrency slot is acquired and the turn
            # actually launches (not while it is still queued on the semaphore), so a voice cut at a budget
            # deadline before it ever ran never emits a misleading "started".
            nonlocal started
            if started:
                return
            started = True
            emit_activity(
                on_activity,
                ActivityEvent(
                    kind=ActivityEventKind.VOICE_STARTED,
                    correlation_id=correlation_id,  # the stable per-voice key
                    cli=req.target.cli,
                    model=req.target.model,
                    role=req.role,
                    depth=base_depth,
                    status="started",
                    message=f"{req.target.display_label} started",
                ),
            )

        # Gate the ACP turn on the global concurrency semaphore so a wide panel cannot launch more than
        # ``max_concurrency`` live sessions at once. Held only around the turn, not the pure guards above.
        async with self._semaphore:
            on_launch()
            result = await run_acp_turn(
                descriptor,
                prompt,
                policy=policy,
                cwd=cwd,
                timeout_s=timeout,
                model=req.target.model,
                effort=self.resolve_effort(req.target.cli, req.effort),
                base_depth=base_depth,
                parent_run_id=req.parent_run_id,
            )
        # N1 (decision 3-A): every delegation launches one subprocess today; a fallback re-run would add to
        # this once cross-target fallback lands, so a panel's realized fan-out already counts per-voice here.
        result.delegation_call_count = 1
        emit_activity(on_activity, _voice_finished_event(result, req.role, base_depth, correlation_id))
        return result

    def resolve_effort(self, cli: str, effort: Effort | None) -> Effort | None:
        """The reasoning-effort tier a ``cli`` voice runs with (F8a, 2-L): the call value, else the config default.

        The single resolution rule -- call ``effort`` wins, else the per-agent ``[agents.<id>] effort``, else
        the global ``default_effort``, else ``None`` (let the agent decide). Shared by the delegation primitive
        and the panels (consensus/debate read it for each voice's rollup, including a voice cut at a deadline),
        so the precedence can never silently diverge across paths.
        """
        return effort if effort is not None else self._config.effort_for(cli)

    def _workspace_trusted(self, req: DelegationRequest) -> bool:
        """Whether a mutating delegation is permitted for ``req``'s working directory."""
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


def _voice_finished_event(result: DelegationResult, role: str | None, depth: int, correlation_id: str) -> ActivityEvent:
    """Build the ``voice_finished`` :class:`ActivityEvent` for a finished delegation (N1, item 3).

    ``status`` distinguishes a clean ``ok`` from a ``cut`` (a time-budget harvest, ``stop_reason='budget'``)
    and a plain ``failed``, so the push side can colour the outcome without re-reading the result.
    ``correlation_id`` is the stable per-voice key so this terminal event collapses onto the same activity
    row as its ``voice_started``.
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


def _compose_prompt(prompt: str, files: list[str]) -> str:
    """Append an in-scope file list to the prompt (ACP resource blocks are a later refinement)."""
    if not files:
        return prompt
    listing = "\n".join(f"- {path}" for path in files)
    return f"{prompt}\n\nFiles in scope:\n{listing}"


def _fail(
    req: DelegationRequest, code: ErrorCode, message: str, *, details: dict[str, object] | None = None
) -> DelegationResult:
    """Build a failed result from an up-front guard, carrying the request's target and safety mode."""
    return DelegationResult(
        target=Target(cli=req.target.cli, model=req.target.model),
        ok=False,
        error=ErrorInfo(code=code, message=message, details=details),
        safety_mode=req.safety_mode,
    )
