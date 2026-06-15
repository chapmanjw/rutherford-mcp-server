# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``continue_job`` tool: build a new run on top of a completed durable job (item 9).

A persisted run (F2) is not a dead end: ``continue_job`` reads its record, then re-issues the agent with a
new direction -- resuming the exact ACP session where the adapter supports it, else re-injecting the prior
prompt + answer as context. The continuation is itself a fresh top-level run that LINKS forward to the run
it built on (``continued_from``), so the chain is traceable without ever mutating the parent (9-B). The
trust gate is re-applied fresh and defaults to ``read_only`` -- a continuation does not inherit the parent's
write mode (9-D), since the new direction may change intent.

It continues a ``delegate`` job (resume the one session, or re-inject), a ``consensus`` panel (resume each
voice's session and re-aggregate under the recorded strategy), or a ``debate`` (resume each seat's session
and argue ``rounds`` more rounds). A panel continuation reuses the persisted :class:`PanelInputs` -- roster,
strategy, stances, per-seat steering, the per-seat resume handles -- so it picks up exactly where the kept
panel left off; a seat whose agent cannot reload its session is recorded as a failed voice, never silently
dropped.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ..context import AppContext, tool_success
from ..domain.enums import Effort, SafetyMode, Strategy
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import (
    ConsensusRequest,
    ConsensusResult,
    DebateRequest,
    DebateResult,
    DelegationRequest,
    DelegationResult,
    PanelInputs,
    RunRecord,
    StrategyResult,
    Target,
)
from ..io.ledger import read_answer, read_record
from ..services.delegation import ActivityCallback
from .common import (
    apply_role,
    as_target,
    ensure_known_agent,
    parse_effort,
    parse_strategy,
    resolve_run_mode,
    resolve_safety_mode,
)
from .jobs import make_summary, submit_job


async def continue_job_tool(
    app: AppContext,
    *,
    job_id: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    trust_workspace: bool = False,
    role: str | None = None,
    effort: str | None = None,
    rounds: int = 2,
    persist: bool = True,
    mode: str = "sync",
) -> str:
    """Continue the persisted job ``job_id`` with a new ``prompt``, picking up where the kept run left off.

    ``job_id`` is the id of a durable run kept under ``<jobs_dir>/`` (the ``run_dir`` name a persisted result
    carries). A ``delegate`` job resumes its one session (else re-injects the prior prompt + answer); a
    ``consensus`` panel resumes each voice's session and re-aggregates under the recorded strategy; a
    ``debate`` resumes each seat's session and argues ``rounds`` MORE rounds (``rounds`` is ignored for the
    other kinds). The parent's record supplies the roster, model, working dir, role, files, and -- for a
    panel -- the strategy / stances / per-seat steering, all inherited unless overridden here. A seat whose
    agent cannot reload its ACP session is recorded as a failed voice, never silently dropped. The
    continuation persists as a fresh run linked to the parent (``continued_from``); it never mutates the
    parent (9-B). The trust gate is fresh and defaults to ``read_only`` -- the parent's write mode is NOT
    inherited (9-D); panels are read-only deliberation regardless. ``mode="async"`` runs it as a background
    job and returns a ``job_id``.
    """
    parent = _read_parent(app, job_id)
    safety = resolve_safety_mode(safety_mode, SafetyMode.READ_ONLY)  # 9-D: read_only default, not parent/config
    cwd = working_dir if working_dir is not None else parent.cwd
    inherited_files = list(files) if files is not None else list(parent.files)
    inherited_role = role if role is not None else parent.role
    resolved_effort = parse_effort(effort)

    if parent.kind == "delegate":
        ensure_known_agent(app.descriptors, parent.cli)
        plan = _ContinuationPlan(
            app=app,
            job_id=job_id,
            new_prompt=prompt,
            target=Target(cli=parent.cli, model=model if model is not None else parent.model),
            cwd=cwd,
            files=inherited_files,
            role=inherited_role,
            safety=safety,
            timeout_s=timeout_s,
            trust_workspace=trust_workspace,
            effort=resolved_effort,
            persist=persist,
            parent=parent,
        )

        async def run(on_activity: ActivityCallback | None = None) -> str:
            result, how = await plan.execute(on_activity)
            result.notice = f"continued job {job_id}: {how}."
            return tool_success(result)

        summary_target = plan.target.display_label
    elif parent.kind in ("consensus", "debate"):
        panel = _require_panel(parent)
        request = _panel_continuation_request(
            parent.kind,
            panel,
            job_id,
            prompt,
            model,
            cwd,
            inherited_files,
            inherited_role,
            safety,
            resolved_effort,
            timeout_s,
            rounds,
            persist,
        )

        async def run(on_activity: ActivityCallback | None = None) -> str:
            if isinstance(request, ConsensusRequest):
                result: ConsensusResult | StrategyResult | DebateResult = await app.consensus.consensus(
                    request, on_activity=on_activity
                )
            else:
                result = await app.debate.debate(request, on_activity=on_activity)
            resumed = sum(1 for seat in panel.targets if seat.session_id is not None)
            result.notice = f"continued {parent.kind} job {job_id}: resumed {resumed} of {len(panel.targets)} seat(s)."
            return tool_success(result)

        summary_target = ", ".join(seat.cli for seat in panel.targets)
    else:
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"cannot continue a {parent.kind!r} job")

    if resolve_run_mode(mode):
        return await submit_job(
            app, "continue_job", run, summary=make_summary("continue_job", target=summary_target, prompt=prompt)
        )
    return await run()


def _require_panel(parent: RunRecord) -> PanelInputs:
    """The persisted panel orchestration config, or a clean error if the record has none (a corrupt panel)."""
    if parent.panel is None:
        raise RutherfordError(
            ErrorCode.INVALID_INPUT, f"the {parent.kind!r} record has no persisted panel inputs to continue from"
        )
    return parent.panel


def _panel_continuation_request(
    kind: str,
    panel: PanelInputs,
    job_id: str,
    prompt: str,
    model: str | None,
    cwd: str | None,
    files: list[str],
    role: str | None,
    safety: SafetyMode,
    effort: Effort | None,
    timeout_s: float | None,
    rounds: int,
    persist: bool,
) -> ConsensusRequest | DebateRequest:
    """Rebuild a consensus / debate request from a persisted panel, with each seat's resume handle threaded in.

    The roster, per-seat steering (stance / weight / parity / role), the aggregation strategy + verdict schema
    (consensus), the synthesize flag and judge are replayed from :class:`PanelInputs`; the new ``prompt`` is
    the continuation direction every resumed seat receives, and ``resume_session_ids`` carries each seat's
    prior session so the agents pick up their own reasoning. ``model`` overrides every seat's model when given.
    """
    targets = [
        Target(
            cli=seat.cli,
            model=model if model is not None else seat.model,
            stance=seat.stance,
            weight=seat.weight,
            parity=seat.parity,
            role=seat.role,
        )
        for seat in panel.targets
    ]
    resume_session_ids: list[str | None] = [seat.session_id for seat in panel.targets]
    judge = as_target(panel.judge) if panel.judge else None
    if kind == "consensus":
        return ConsensusRequest(
            targets=targets,
            prompt=prompt,
            strategy=parse_strategy(panel.strategy) if panel.strategy else Strategy.ALL_VOICES,
            verdict_schema=panel.verdict_schema,
            synthesize=panel.synthesize,
            judge=judge,
            working_dir=cwd,
            files=files,
            role=role,
            safety_mode=safety,
            effort=effort,
            timeout_s=timeout_s,
            resume_session_ids=resume_session_ids,
            continued_from=job_id,
            persist=persist,
        )
    return DebateRequest(
        targets=targets,
        prompt=prompt,
        rounds=rounds,
        synthesize=panel.synthesize if panel.synthesize is not None else True,
        judge=judge,
        working_dir=cwd,
        files=files,
        role=role,
        safety_mode=safety,
        effort=effort,
        timeout_s=timeout_s,
        resume_session_ids=resume_session_ids,
        continued_from=job_id,
        persist=persist,
    )


class _ContinuationPlan:
    """The resolved inputs for one continuation, with the resume-then-reinject execution policy (9-A/9-E)."""

    def __init__(
        self,
        *,
        app: AppContext,
        job_id: str,
        new_prompt: str,
        target: Target,
        cwd: str | None,
        files: list[str],
        role: str | None,
        safety: SafetyMode,
        timeout_s: float | None,
        trust_workspace: bool,
        effort: Effort | None,
        persist: bool,
        parent: RunRecord,
    ) -> None:
        self._app = app
        self._job_id = job_id
        self._new_prompt = new_prompt
        self.target = target
        self._cwd = cwd
        self._files = files
        self._role = role
        self._safety = safety
        self._timeout_s = timeout_s
        self._trust_workspace = trust_workspace
        self._effort = effort
        self._persist = persist
        self._parent = parent

    async def execute(self, on_activity: ActivityCallback | None) -> tuple[DelegationResult, str]:
        """Resume the parent's session when one was recorded, falling back to re-injection on RESUME_FAILED."""
        if self._parent.session_id:
            result = await self._delegate(self._new_prompt, self._parent.session_id, on_activity)
            if not (result.error is not None and result.error.code is ErrorCode.RESUME_FAILED):
                return result, "resumed the agent session"
            # The adapter cannot reload its own sessions: the resume miss is a cheap handshake-only failure;
            # fall back to re-injecting the prior context as the real continuation (9-E). Drop the failed
            # resume's stray record so the continuation chain keeps only the one real child (9-B).
            if result.run_dir is not None:
                self._app.ledger.remove(Path(result.run_dir).name)
            return await self._reinject(on_activity), (
                "re-injected the prior prompt and answer as context (the agent does not support resume)"
            )
        return await self._reinject(on_activity), (
            "re-injected the prior prompt and answer as context (no resumable session was recorded)"
        )

    async def _reinject(self, on_activity: ActivityCallback | None) -> DelegationResult:
        answer = read_answer(self._app.ledger.root / self._job_id)
        prompt = _reinjection_prompt(self._parent.prompt, answer, self._new_prompt)
        return await self._delegate(prompt, None, on_activity)

    async def _delegate(
        self, prompt: str, session_id: str | None, on_activity: ActivityCallback | None
    ) -> DelegationResult:
        request = DelegationRequest(
            target=self.target,
            prompt=apply_role(self._app.roles, self._role, prompt),
            working_dir=self._cwd,
            files=self._files,
            role=self._role,
            safety_mode=self._safety,
            timeout_s=self._timeout_s,
            trust_workspace=self._trust_workspace,
            effort=self._effort,
            persist=self._persist,
            session_id=session_id,
            continues_run_id=self._job_id,
        )
        return await self._app.delegation.delegate(request, correlation_id="continue:0", on_activity=on_activity)


def _read_parent(app: AppContext, job_id: str) -> RunRecord:
    """Load the parent run's record, mapping a missing/corrupt/escaping id to a clean error.

    Guards against a ``job_id`` that escapes the jobs root (``..`` / a path separator) -- the id must be a
    single directory name -- so a continuation can never read an arbitrary file off disk. A second,
    resolve-and-contain check rejects even a single-component id whose entry is a symlink pointing OUTSIDE the
    root, so the continuation can only ever read a record the ledger itself wrote.
    """
    if not job_id or Path(job_id).name != job_id or job_id in (".", ".."):
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"invalid job id {job_id!r}")
    run_dir = app.ledger.root / job_id
    if not run_dir.resolve().is_relative_to(app.ledger.root.resolve()):
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"job id {job_id!r} resolves outside the jobs root")
    try:
        return read_record(run_dir)
    except OSError as exc:
        raise RutherfordError(ErrorCode.JOB_NOT_FOUND, f"no persisted job {job_id!r} under {app.ledger.root}") from exc
    except (ValidationError, ValueError) as exc:  # ValueError covers json.JSONDecodeError
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"the record for job {job_id!r} is corrupt: {exc}") from exc


def _reinjection_prompt(parent_prompt: str, parent_answer: str, new_prompt: str) -> str:
    """Re-inject the parent's prompt (+ answer, when present) as context ahead of the new direction (9-E)."""
    parts = ["You previously worked on this task in an earlier session:", "", parent_prompt.strip()]
    if parent_answer.strip():
        parts += ["", "Your prior answer was:", "", parent_answer.strip()]
    parts += ["", "Now continue with this new direction:", "", new_prompt.strip()]
    return "\n".join(parts)
