# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The delegation service: hand one request to one ACP agent and return the normalized envelope.

The ACP-native foundational primitive every tool bottoms out in. It resolves the agent descriptor, builds
the :class:`~rutherford.acp.permission.PermissionPolicy` from the safety mode (guarding the mutating modes
behind a trusted-workspace check), composes the prompt with any in-scope files, and drives one ACP turn via
:func:`~rutherford.acp.session.run_acp_turn`. Every operational failure is returned as a structured
:class:`DelegationResult`, never raised, so a consensus panel never aborts on one bad voice.
"""

from __future__ import annotations

from pathlib import Path

from ..acp.descriptors import DescriptorRegistry
from ..acp.permission import PermissionPolicy
from ..acp.session import run_acp_turn
from ..config.schema import RutherfordConfig
from ..domain.enums import Effort, is_mutating
from ..domain.error_codes import ErrorCode
from ..domain.models import DelegationRequest, DelegationResult, ErrorInfo, Target


class DelegationService:
    """Executes a single ACP delegation end to end."""

    def __init__(self, descriptors: DescriptorRegistry, config: RutherfordConfig) -> None:
        self._descriptors = descriptors
        self._config = config

    async def delegate(self, req: DelegationRequest) -> DelegationResult:
        """Run ``req`` against its target agent and return the normalized result."""
        if not self._descriptors.has(req.target.cli):
            known = ", ".join(self._descriptors.ids()) or "(none)"
            return _fail(req, ErrorCode.UNKNOWN_TARGET, f"unknown agent id {req.target.cli!r}; known agents: {known}")
        descriptor = self._descriptors.get(req.target.cli)

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
        return await run_acp_turn(
            descriptor,
            prompt,
            policy=policy,
            cwd=cwd,
            timeout_s=timeout,
            model=req.target.model,
            effort=self.resolve_effort(req.target.cli, req.effort),
        )

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


def _compose_prompt(prompt: str, files: list[str]) -> str:
    """Append an in-scope file list to the prompt (ACP resource blocks are a later refinement)."""
    if not files:
        return prompt
    listing = "\n".join(f"- {path}" for path in files)
    return f"{prompt}\n\nFiles in scope:\n{listing}"


def _fail(req: DelegationRequest, code: ErrorCode, message: str) -> DelegationResult:
    """Build a failed result from an up-front guard, carrying the request's target and safety mode."""
    return DelegationResult(
        target=Target(cli=req.target.cli, model=req.target.model),
        ok=False,
        error=ErrorInfo(code=code, message=message),
        safety_mode=req.safety_mode,
    )
