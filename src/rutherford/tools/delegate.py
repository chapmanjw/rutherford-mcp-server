# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``delegate`` tool: hand one task to one CLI."""

from __future__ import annotations

from ..context import AppContext, tool_success
from ..domain.enums import DelegationMode, is_mutating
from ..domain.models import DelegationRequest, Target
from .common import as_target, async_job_envelope, parse_effort, parse_mode, resolve_safety_mode


async def delegate_tool(
    app: AppContext,
    *,
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    mode: str = "sync",
    timeout_s: float | None = None,
    effort: str | None = None,
    session_id: str | None = None,
    include_raw: bool = False,
    trust_workspace: bool = False,
    persist: bool | None = None,
    external_tracking: bool = False,
    fallback: list[str] | None = None,
) -> str:
    """Delegate ``prompt`` to ``(cli, model)`` and return the normalized result.

    With ``mode="async"`` the call returns a job id immediately; poll ``job_status`` /
    ``job_result``. A delegation that fails operationally (missing binary, timeout, non-zero exit)
    returns a result with ``ok=false`` and an error code, not an exception. ``fallback`` is an ordered
    list of alternate ``cli`` / ``cli:model`` targets to try if the primary fails on a retryable
    category (F7). ``persist`` keeps the run as a durable job under ``.rutherford/jobs/<id>/`` (its
    ``run_dir`` is returned on the result); ``None`` follows the configured ``default_persistence``.
    ``effort`` (``low`` | ``medium`` | ``high`` | ``xhigh``) is the producer "how hard may it think" hint,
    mapped to the CLI's native knob and reported as ``effort_applied`` (F8a); omit it to follow the
    configured ``default_effort``. The wall-clock ``time_budget_s`` harvest is a panel feature (see
    ``consensus`` / ``debate``): a single delegation has no fan-out to harvest, so it is the degenerate
    case that "collapses toward timeout" (F8a, 2-behavior) -- ``timeout_s`` is the per-call bound, and a
    run that hits it keeps the stdout it streamed before the cut on the result's ``partial`` rather than
    discarding it.
    """
    request = DelegationRequest(
        target=Target(cli=cli, model=model),
        prompt=prompt,
        working_dir=working_dir,
        files=files or [],
        role=role,
        safety_mode=resolve_safety_mode(safety_mode, app.config.default_safety_mode),
        mode=parse_mode(mode),
        timeout_s=timeout_s,
        effort=parse_effort(effort),
        session_id=session_id,
        include_raw=include_raw,
        trust_workspace=trust_workspace,
        persist=persist,
        fallback=[as_target(entry) for entry in (fallback or [])],
    )
    correlation_id = app.new_correlation_id()
    # A non-trivial delegation worth a suggest-a-job nudge (1-J): a mutating run, or a multi-target run --
    # a fallback chain names alternate targets, so it is no longer a "plain single-target" delegation.
    complex_run = is_mutating(request.safety_mode) or bool(request.fallback)

    if request.mode is DelegationMode.ASYNC:
        job = app.jobs.submit(
            "delegate",
            # A single delegation has no fan-out to harvest, so it publishes no interim result (the second
            # body arg is unused); time-budget continue/harvest is a panel feature.
            lambda progress, _set_interim: app.delegation.delegate(
                request,
                correlation_id=correlation_id,
                base_depth=app.base_depth,
                on_progress=progress,
            ),
        )
        return tool_success(
            async_job_envelope(
                app,
                job,
                persist=request.persist,
                complex_run=complex_run,
                external_tracking=external_tracking,
            )
        )

    result = await app.delegation.delegate(request, correlation_id=correlation_id, base_depth=app.base_depth)
    result.notice = app.persistence_notice(
        persisted=result.run_dir is not None,
        complex_run=complex_run,
        external_tracking=external_tracking,
    )
    return tool_success(result)
