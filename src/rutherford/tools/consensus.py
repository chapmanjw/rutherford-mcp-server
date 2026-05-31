# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``consensus`` tool: ask the same prompt of several CLIs in parallel."""

from __future__ import annotations

from typing import Any

from ..context import AppContext, tool_success
from ..domain.enums import DelegationMode
from ..domain.models import ConsensusRequest
from .common import as_target, parse_mode, parse_safety_mode, parse_stances


async def consensus_tool(
    app: AppContext,
    *,
    targets: list[Any],
    prompt: str,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str = "read_only",
    synthesize: bool = False,
    timeout_s: float | None = None,
    mode: str = "sync",
    include_raw: bool = False,
) -> str:
    """Run ``prompt`` across ``targets`` in parallel and return every voice.

    ``targets`` is a list of ``{cli, model}`` objects (or ``cli`` / ``cli:model`` strings).
    Optional ``stances`` (parallel to ``targets``) steer each voice for/against/neutral. Optional
    ``synthesize`` adds a server-side combined answer; it is off by default.
    """
    request = ConsensusRequest(
        targets=[as_target(target) for target in targets],
        prompt=prompt,
        stances=parse_stances(stances),
        working_dir=working_dir,
        files=files or [],
        role=role,
        safety_mode=parse_safety_mode(safety_mode),
        synthesize=synthesize,
        timeout_s=timeout_s,
        include_raw=include_raw,
        depth=app.base_depth,
    )
    correlation_id = app.new_correlation_id()

    if parse_mode(mode) is DelegationMode.ASYNC:
        job = app.jobs.submit(
            "consensus",
            lambda progress: app.consensus.consensus(
                request,
                correlation_id=correlation_id,
                base_depth=app.base_depth,
                on_progress=progress,
            ),
        )
        return tool_success({"job_id": job.id, "status": job.status, "kind": job.kind})

    result = await app.consensus.consensus(request, correlation_id=correlation_id, base_depth=app.base_depth)
    return tool_success(result)
