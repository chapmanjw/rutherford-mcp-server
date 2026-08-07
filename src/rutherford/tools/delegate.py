# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``delegate`` tool: hand a task to one ACP agent and return its normalized result."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.enums import is_mutating
from ..domain.models import DelegationRequest, Target
from ..runtime.depth import current_depth
from ..services.delegation import ActivityCallback
from .common import (
    apply_role,
    as_target,
    ensure_known_agent,
    ensure_known_targets,
    parse_effort,
    resolve_run_mode,
    resolve_safety_mode,
)
from .jobs import make_summary, submit_job


async def delegate_tool(
    app: AppContext,
    *,
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    trust_workspace: bool = False,
    direct_workspace_mutation: bool = False,
    role: str | None = None,
    effort: str | None = None,
    fallback: list[Any] | None = None,
    allow_model_fallback: bool = True,
    persist: bool | None = None,
    session_id: str | None = None,
    external_tracking: bool = False,
    mode: str = "sync",
) -> str:
    """Validate the request, drive one ACP turn (with fallback), and return the TOON-encoded result envelope.

    ``mode="async"`` submits the turn as a background job and returns a ``job_id`` immediately (poll it
    with ``job_status`` / ``job_result``); ``mode="sync"`` (the default) awaits and returns the result.
    Validation (known agent, safety mode, run mode, role, effort, fallback targets) always runs
    synchronously, so a bad request fails on the request path rather than inside a job. A named ``role`` has
    its persona prepended to ``prompt`` before the request is built; ``UNKNOWN_ROLE`` if the id is not a known
    role. ``effort`` (low|medium|high|xhigh) asks the agent to spend more reasoning where it has a knob (a
    reported no-op otherwise); omitted, the per-agent or global ``default_effort`` applies.

    ``fallback`` is an ordered list of alternate targets (``cli`` / ``cli:model`` strings, or ``{cli, model}``
    objects) to try when the primary delegation fails on a re-execution-SAFE failure (a spawn/handshake
    failure that never ran the prompt); a benched (cooled-down) alternate is skipped and the first one that
    answers becomes the result, with ``fallback_chain`` recording the failures along the way. A write/yolo
    delegation never falls back (a partial mutation may have happened). ``allow_model_fallback`` (on by
    default) lets a model-unavailable failure retry the SAME agent on its configured ``fallback_model`` first,
    where it has one (most ACP agents do not -- a clean no-op).

    ``direct_workspace_mutation=True`` asks for a ``write`` / ``yolo`` agent to edit ``working_dir`` itself,
    with live terminal access there, instead of an isolated worktree / temp copy. Nothing is captured and
    nothing is applied back, so the result carries no diff and no changed-file list -- only
    ``direct_mutation=True`` to say why they are missing.

    Asking never suffices, because the caller is the party least able to judge the risk. The operator must
    have set ``allow_direct_workspace_mutation`` in config, ``working_dir`` must be explicit and on the
    configured ``trusted_workspaces`` allowlist (a per-call ``trust_workspace`` does NOT qualify), and the
    call must not be nested inside another delegation. ``propose`` cannot use it at all
    (``INVALID_INPUT`` -- its diff IS the sandbox); ``read_only`` already runs in place, so it is a no-op.

    ``persist`` keeps this run as a durable job under ``<jobs_dir>/<run_id>/`` (F2: ``state.json`` plus the
    answer / diff artifacts), so it survives the process. ``None`` follows the configured
    ``default_persistence`` (``ephemeral`` out of the box -- nothing on disk unless asked); ``True`` / ``False``
    force it for this one call. The persisted result carries its ``run_dir``.

    ``session_id`` resumes a prior agent session: pass the ``session_id`` from an earlier delegate result and
    the agent reloads that conversation (ACP ``session/load``) instead of starting fresh, so a follow-up turn
    continues where the last left off. Only agents that persist their own sessions support it; against one that
    does not the call fails ``RESUME_FAILED``. The resume restores the conversation, not the filesystem -- a
    write/yolo resume still runs in a fresh isolated sandbox.
    """
    ensure_known_agent(app.descriptors, cli)
    safety = resolve_safety_mode(safety_mode, app.config.default_safety_mode)
    run_async = resolve_run_mode(mode)
    composed_prompt = apply_role(app.roles, role, prompt)
    fallback_targets = [as_target(target) for target in fallback] if fallback else []
    ensure_known_targets(app.descriptors, fallback_targets)
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=composed_prompt,
        working_dir=working_dir,
        files=list(files) if files else [],
        role=role,
        safety_mode=safety,
        timeout_s=timeout_s,
        trust_workspace=trust_workspace,
        direct_workspace_mutation=direct_workspace_mutation,
        effort=parse_effort(effort),
        fallback=fallback_targets,
        allow_model_fallback=allow_model_fallback,
        persist=persist,
        session_id=session_id,
    )

    async def run(on_activity: ActivityCallback | None = None) -> str:
        # A standalone delegation emits one voice_started/voice_finished pair (N1, item 3): on the async path
        # the job buffers them for the ``activity`` poll table; on the sync path there is no sink (None).
        # Seed the depth from the environment, not from zero. A nested Rutherford -- an agent that is
        # itself running one of these servers -- inherits RUTHERFORD_DEPTH from its parent, and reading it
        # back here is what makes the recursion cap and the direct-mutation nesting refusal real across a
        # process boundary rather than only within one.
        result = await app.delegation.delegate(
            request, correlation_id="voice:0", base_depth=current_depth(), on_activity=on_activity
        )
        # Advisory F2 nudge (suppressed by external_tracking): a mutating or fallback delegation is worth
        # keeping as a durable job, plus the one-time first-run setup hint.
        result.notice = app.persistence_notice(
            persisted=result.run_dir is not None,
            complex_run=is_mutating(request.safety_mode) or bool(request.fallback),
            external_tracking=external_tracking,
        )
        return tool_success(result)

    if run_async:
        summary = make_summary("delegate", target=request.target.display_label, prompt=prompt)
        return await submit_job(app, "delegate", run, summary=summary)
    return await run()
