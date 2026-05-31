# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``review`` tool: a read-only code review, built on consensus."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ConsensusRequest
from .common import as_target, parse_safety_mode


async def review_tool(
    app: AppContext,
    *,
    targets: list[Any],
    paths: list[str] | None = None,
    diff: str | None = None,
    role: str = "codereviewer",
    working_dir: str | None = None,
    safety_mode: str = "read_only",
    synthesize: bool = False,
    timeout_s: float | None = None,
) -> str:
    """Review a diff or a set of files across one or more targets and return every voice.

    Built on the consensus service with the ``codereviewer`` role; review is read-only by nature.
    Provide either ``diff`` (a unified diff) or ``paths`` (files for the agents to read).
    """
    if not diff and not paths:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "review needs either 'diff' or 'paths'")

    request = ConsensusRequest(
        targets=[as_target(target) for target in targets],
        prompt=_review_prompt(diff),
        role=role,
        files=paths or [],
        working_dir=working_dir,
        safety_mode=parse_safety_mode(safety_mode),
        synthesize=synthesize,
        timeout_s=timeout_s,
        depth=app.base_depth,
    )
    result = await app.consensus.consensus(request, correlation_id=app.new_correlation_id(), base_depth=app.base_depth)
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
