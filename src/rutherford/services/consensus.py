# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The consensus service: ask several ACP agents the same prompt in parallel and return every voice.

Where the delegation service hands a prompt to one agent, consensus fans it out to N agents concurrently
(each its own ACP session) and returns the normalized voices. One failing voice is a failed
:class:`DelegationResult` in the result, never an aborted panel. The per-call target cap bounds the
fan-out; richer aggregation (strategies, synthesis, diversity) is re-added in a later phase.
"""

from __future__ import annotations

import asyncio

from ..config.schema import RutherfordConfig
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import ConsensusRequest, ConsensusResult, DelegationRequest, DelegationResult, Target
from .delegation import DelegationService


class ConsensusService:
    """Runs the same prompt across several ACP agents in parallel and returns every voice."""

    def __init__(self, delegation: DelegationService, config: RutherfordConfig) -> None:
        self._delegation = delegation
        self._config = config

    async def consensus(self, req: ConsensusRequest) -> ConsensusResult:
        """Delegate ``req``'s prompt to every target concurrently and return the collected voices."""
        if not req.targets:
            raise RutherfordError(ErrorCode.INVALID_INPUT, "consensus needs at least one target")
        if len(req.targets) > self._config.max_targets:
            raise RutherfordError(
                ErrorCode.TOO_MANY_TARGETS,
                f"consensus requested {len(req.targets)} targets; the per-call cap is {self._config.max_targets}",
            )

        async def _one(target: Target) -> DelegationResult:
            request = DelegationRequest(
                target=target,
                prompt=req.prompt,
                working_dir=req.working_dir,
                files=req.files,
                safety_mode=req.safety_mode,
                timeout_s=req.timeout_s,
            )
            return await self._delegation.delegate(request)

        voices = await asyncio.gather(*(_one(target) for target in req.targets))
        return ConsensusResult(voices=list(voices))
