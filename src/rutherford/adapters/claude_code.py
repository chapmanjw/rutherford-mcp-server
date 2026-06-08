# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Claude Code adapter -- the reference implementation.

Invocation: ``claude -p "<prompt>" --output-format json`` with ``--model``, ``--add-dir``,
``--append-system-prompt`` (for a role preamble), ``--resume <id>``, and the safety flags below.
Auth is a subscription/OAuth session or ``ANTHROPIC_API_KEY`` -- or a third-party backend (AWS
Bedrock / Google Vertex), whose AWS/GCP credential chain a cheap probe cannot verify, so that case
defers to a live check. The JSON output carries the final ``result`` text, a ``session_id`` for
resume, and ``total_cost_usd`` / token usage.

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
        """Resolve auth from ``claude auth status``, deferring a third-party backend to a live check.

        ``claude auth status`` emits a JSON object describing the *effective* auth, e.g.
        ``{"loggedIn": true, "authMethod": "third_party", "apiProvider": "bedrock"}``. The reliable
        signal is that JSON body, not the process exit code. When the backend is a third-party cloud
        (AWS Bedrock / Google Vertex / Bedrock Mantle / Claude Platform on AWS -- flagged by
        ``authMethod == "third_party"``, an ``apiProvider`` like ``bedrock``, or a
        ``CLAUDE_CODE_USE_*`` switch), ``loggedIn`` only means Claude Code is *configured* for it --
        it does not prove the AWS/GCP credential chain is valid and can reach a model. The only
        trustworthy signal there is a live call, so report ``UNKNOWN`` and let ``doctor``'s live
        verification spend one read-only test prompt (see ``tools/capabilities.py``).
        """
        result = self._probe.run([self.binary, "auth", "status"], timeout_s=15.0)
        third_party = self._env_truthy(*_THIRD_PARTY_BACKEND_FLAGS)
        status = _parse_status_json(result.stdout)
        if status is not None:
            provider = str(status.get("apiProvider", "")).strip().lower()
            auth_method = str(status.get("authMethod", "")).strip().lower()
            if third_party or auth_method == "third_party" or provider in _THIRD_PARTY_PROVIDERS:
                return _defer_to_live(provider)
            if status.get("loggedIn"):
                return AuthStatus(state=AuthState.AUTHENTICATED, detail="logged-in session detected")
            return AuthStatus(state=AuthState.NEEDS_LOGIN, detail="not logged in; run `claude auth login`")
        # No parseable status JSON (an older CLI, or a non-JSON message): fall back to the backend
        # flag, then an API key, then the bare exit code -- the pre-JSON-body heuristic.
        if third_party:
            return _defer_to_live("")
        if self._env_present("ANTHROPIC_API_KEY"):
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="ANTHROPIC_API_KEY is set")
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

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful claude run must carry a JSON result object (``--output-format json``).

        The drift canary for this adapter: if a future CLI build stops emitting the JSON envelope
        but still exits cleanly, this is what catches the silent regression at the delegation layer.
        """
        return _last_json_object(raw.stdout) is not None


#: ``CLAUDE_CODE_USE_*`` switches that route Claude Code to a cloud backend whose credential is an
#: AWS/GCP chain -- not an Anthropic key or login -- so a cheap probe cannot verify reachability.
#: All four are documented at code.claude.com (env-vars / amazon-bedrock / google-vertex-ai pages).
_THIRD_PARTY_BACKEND_FLAGS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)

#: ``apiProvider`` values from ``claude auth status`` that name such a cloud backend.
_THIRD_PARTY_PROVIDERS = frozenset({"bedrock", "vertex", "mantle"})


def _defer_to_live(provider: str) -> AuthStatus:
    """Report ``UNKNOWN`` for a third-party backend so ``doctor`` confirms it with a live test prompt.

    A cloud backend's credential (an AWS/GCP chain) can't be verified non-interactively, and a bare
    "the switch is set" check would only prove it is *configured*, not reachable. ``UNKNOWN`` routes
    it into ``doctor``'s live verification while keeping the cheap ``capabilities`` snapshot honest.
    """
    label = provider or "a third-party cloud backend (Bedrock/Vertex)"
    return AuthStatus(
        state=AuthState.UNKNOWN,
        detail=f"configured for {label}; the cloud credential chain can't be verified "
        "non-interactively -- doctor confirms access with a live test prompt",
    )


def _parse_status_json(stdout: str) -> dict[str, Any] | None:
    """Return the JSON object emitted by ``claude auth status``, or ``None`` if absent.

    The output is a single JSON object, but it may be pretty-printed across lines or preceded by
    log noise, so try a whole-output parse first and fall back to the last single-line object.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return whole
    return _last_json_object(stdout)


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
