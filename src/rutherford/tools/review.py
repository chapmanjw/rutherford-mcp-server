# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``review`` tool: a read-only code review across ACP agents, built on consensus."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.enums import SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ConsensusRequest, Target
from .common import apply_role, as_target, ensure_known_targets
from .panels import panel_for_call


async def review_tool(
    app: AppContext,
    *,
    targets: list[Any] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    paths: list[str] | None = None,
    diff: str | None = None,
    role: str = "principal-reviewer",
    working_dir: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
) -> str:
    """Review a diff or a set of files across one or more agents and return every voice.

    Built on the consensus service with the ``principal-reviewer`` role prepended to the review prompt.
    Review is CLAMPED to ``read_only`` -- it takes no ``safety_mode`` so the tool's name stays honest (an
    inspection-named tool must not run with mutating permissions); mutating work runs through ``delegate`` (the
    single sandboxed write path) by design -- the panels (consensus / debate) are read-only deliberation.
    Provide either ``diff`` (a unified diff, inlined into the prompt) or ``paths``
    (files put in scope for the agents to read), and either a list of ``targets`` or a saved ``panel`` (with
    optional ``panel_overrides``); panel and targets are mutually exclusive. ``synthesize`` defaults on (a
    combined verdict is the useful default for a review); pass ``false`` for the raw per-voice reviews.
    Returns the ``ConsensusResult`` / ``StrategyResult`` envelope consensus produces.
    """
    if not diff and not paths:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "review needs either 'diff' or 'paths'")

    review_targets: list[Target]
    if panel is not None:
        review_targets = panel_for_call(app, panel, panel_overrides, targets, None).to_targets()
    else:
        review_targets = [as_target(target) for target in targets or []]
    ensure_known_targets(app.descriptors, review_targets)  # a clean tool-boundary error, not a buried voice

    request = ConsensusRequest(
        targets=review_targets,
        prompt=apply_role(app.roles, role, _review_prompt(diff)),
        role=role,
        files=paths or [],
        working_dir=working_dir,
        safety_mode=SafetyMode.READ_ONLY,
        # Review synthesizes on by default (a combined verdict is the useful default), unlike consensus whose
        # ``synthesize_default`` is off; an explicit ``False`` from the caller still wins.
        synthesize=True if synthesize is None else synthesize,
        timeout_s=timeout_s,
    )
    result = await app.consensus.consensus(request)
    return tool_success(result)


def _review_prompt(diff: str | None) -> str:
    """Build the review instruction; the diff is inlined, file paths arrive via file context."""
    instruction = (
        "Review the code for correctness, security, and clarity. Report findings by file and line, "
        "separating must-fix issues from optional suggestions. If it is sound, say so."
    )
    if diff:
        return f"{instruction}\n\nReview this diff:\n\n```diff\n{diff}\n```"
    return f"{instruction}\n\nRead the files provided below and review them."
