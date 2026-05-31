# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Claude Code adapter -- the reference implementation.

Invocation: ``claude -p "<prompt>" --output-format json`` with ``--model``, ``--add-dir``,
``--append-system-prompt`` (for a role preamble), ``--resume <id>``, and the safety flags below.
Auth is a subscription/OAuth session or ``ANTHROPIC_API_KEY``. The JSON output carries the final
``result`` text, a ``session_id`` for resume, and ``total_cost_usd`` / token usage.

Flags verified 2026-05-30 against ``claude --help`` (Claude Code 2.1.158).
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.enums import AuthState, OutputMode, SafetyMode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    Cost,
    DelegationRequest,
    DelegationResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)
from .base import BaseCLIAdapter
from .results import error_result, nonzero_result, success_result, timeout_result


class ClaudeCodeAdapter(BaseCLIAdapter):
    """Adapter for Anthropic's Claude Code CLI (``claude``)."""

    id = "claude_code"
    display_name = "Claude Code"
    binary = "claude"
    static_models = ("opus", "sonnet", "haiku")

    def check_auth(self) -> AuthStatus:
        if self._env_present("ANTHROPIC_API_KEY"):
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="ANTHROPIC_API_KEY is set")
        result = self._probe.run([self.binary, "auth", "status"], timeout_s=15.0)
        if result.exit_code == 0:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="logged-in session detected")
        return AuthStatus(
            state=AuthState.NEEDS_LOGIN,
            detail="no ANTHROPIC_API_KEY and no logged-in session; run `claude auth login`",
        )

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=True,
            output_mode=OutputMode.JSON,
            file_context_style="add_dir",
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        # In headless -p mode the agent cannot answer approval prompts, so read_only/propose
        # simply run with default permissions: edits are never auto-approved and so are not
        # applied. write auto-approves file edits; yolo bypasses all permission checks.
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["--permission-mode", "acceptEdits"], note="auto-approve file edits")
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--dangerously-skip-permissions"], note="bypass all permission checks")
        return SafetyFlags(args=[], note="default permissions; edits are not auto-approved in headless mode")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        prompt = self._with_files(req.prompt, req.files)
        argv = [self.binary, "-p", prompt, "--output-format", "json"]

        if req.target.model:
            argv += ["--model", req.target.model]
        if ctx.role_preamble:
            argv += ["--append-system-prompt", ctx.role_preamble]
        if req.working_dir:
            argv += ["--add-dir", req.working_dir]
        if req.session_id:
            argv += ["--resume", req.session_id]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        if raw.timed_out:
            return timeout_result(ctx, raw)

        payload = _last_json_object(raw.stdout)
        if payload is None:
            if raw.exit_code != 0:
                return nonzero_result(ctx, raw)
            return error_result(
                ctx,
                raw,
                "PARSE_ERROR",
                "claude --output-format json produced no parseable JSON object",
                text=raw.stdout.strip(),
            )

        text = str(payload.get("result", ""))
        session_id = payload.get("session_id")
        is_error = bool(payload.get("is_error")) or str(payload.get("subtype", "")).startswith("error")
        if raw.exit_code != 0 or is_error:
            message = text or payload.get("subtype") or "claude reported an error"
            return error_result(ctx, raw, "NONZERO_EXIT", str(message), text=text)

        return success_result(
            ctx,
            raw,
            text,
            session_id=str(session_id) if session_id else None,
            cost=_extract_cost(payload),
        )


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    """Return the last line of ``stdout`` that parses as a JSON object, or ``None``."""
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_cost(payload: dict[str, Any]) -> Cost | None:
    """Build a :class:`Cost` from the claude JSON result, if any cost fields are present."""
    usd = payload.get("total_cost_usd")
    usage = payload.get("usage") or {}
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    if usd is None and input_tokens is None and output_tokens is None:
        return None
    return Cost(usd=usd, input_tokens=input_tokens, output_tokens=output_tokens)
