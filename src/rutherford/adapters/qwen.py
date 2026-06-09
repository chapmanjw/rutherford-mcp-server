# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Qwen Code adapter (``qwen``), a gemini-cli fork.

Invocation: ``qwen -o json`` with the prompt read from **stdin** (not argv), because on Windows
``qwen`` is an npm shim and passing the prompt as an argument invites shell-quoting trouble.
``-m`` selects a model, ``--append-system-prompt`` carries the role preamble (qwen has a
system-prompt flag, so the preamble is never folded into the prompt), ``--add-dir`` widens the
in-scope directory, ``-r <id>`` resumes a session, and ``--approval-mode`` sets the safety
posture (see :meth:`QwenAdapter.map_safety`). qwen has no working-directory flag, so the working
dir is set on the spec's ``cwd`` and also passed via ``--add-dir``.

``-o json`` emits a JSON **array** of event objects. The final answer is the last element with
``"type" == "result"`` (fields ``result``, ``session_id``, ``is_error``, ``usage``); if there is
no result element, we fall back to the last ``{"type":"assistant"}`` message's text. Auth is
qwen-oauth (the default, with no non-interactive check) or an ``OPENAI_API_KEY`` /
``DASHSCOPE_API_KEY`` env var.

Flags verified 2026-05-30 against ``qwen --help`` (qwen 0.17.0).
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


class QwenAdapter(BaseCLIAdapter):
    """Adapter for the Qwen Code CLI (``qwen``), a gemini-cli fork."""

    id = "qwen"
    display_name = "Qwen Code"
    binary = "qwen"
    static_models: tuple[str, ...] = ()
    version_args = ("--version",)

    def check_auth(self) -> AuthStatus:
        """Report auth from an API-key env var; otherwise UNKNOWN (qwen-oauth has no probe).

        qwen defaults to qwen-oauth, which has no non-interactive whoami, so a missing env key
        does not mean the user is logged out. Return UNKNOWN rather than NEEDS_LOGIN and let
        ``doctor`` confirm oauth with a live round trip.
        """
        present = self._env_present("OPENAI_API_KEY", "DASHSCOPE_API_KEY")
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="qwen-oauth has no non-interactive check; doctor verifies it with a live round trip",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise qwen's feature flags (JSON output, resume, model/system-prompt selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=False,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=True,
            output_mode=OutputMode.JSON,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to a qwen ``--approval-mode``, defaulting conservatively.

        qwen's modes are ``plan|default|auto-edit|auto|yolo``. ``default`` prompts and would
        hang headless, so it is never used. read_only and propose use ``plan`` (no edits); write
        uses ``auto-edit`` (auto-approve edit tools); yolo uses ``yolo`` (auto-approve all). The
        fall-through default is ``plan``, never a bypass mode.
        """
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["--approval-mode", "auto-edit"], note="auto-approve edit tools")
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--approval-mode", "yolo"], note="auto-approve all tools")
        return SafetyFlags(args=["--approval-mode", "plan"], note="plan only; no edits applied")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``qwen -o json`` invocation, with the composed prompt fed via stdin.

        Pure: returns an argv list and an stdin string, never a shell command line. qwen has a
        system-prompt flag, so the role preamble rides in ``--append-system-prompt`` and is not
        prepended to the prompt; only the in-scope file list is folded into the stdin prompt.
        """
        prompt = self._with_files(req.prompt, req.files)

        argv = [self.binary, "-o", "json"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        if req.target.model:
            argv += ["-m", req.target.model]
        if ctx.role_preamble:
            argv += ["--append-system-prompt", ctx.role_preamble]
        if req.working_dir:
            argv += ["--add-dir", req.working_dir]
        if req.session_id:
            argv += ["-r", req.session_id]

        return InvocationSpec(
            argv=argv,
            env=dict(safety.env),
            cwd=req.working_dir,
            stdin=prompt,
        )

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSON event array into the normalized envelope, defensively.

        ``-o json`` prints a JSON array of event objects. The answer is the last ``result``
        event (or, failing that, the last ``assistant`` message's text). A ``result`` with
        ``is_error`` true, or a non-zero exit, yields a failure; output that is not a JSON array
        with usable text yields a PARSE_ERROR. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        events = _parse_events(raw.stdout)
        if events is None:
            if raw.exit_code != 0:
                return nonzero_result(ctx, raw)
            return error_result(
                ctx,
                raw,
                "PARSE_ERROR",
                "qwen -o json produced no parseable JSON array",
                text=raw.stdout.strip(),
            )

        result_event = _last_event_of_type(events, "result")
        if result_event is not None:
            raw_answer = result_event.get("result")
            text = str(raw_answer) if isinstance(raw_answer, str) else ""
            session_id = result_event.get("session_id")
            is_error = bool(result_event.get("is_error"))
            if raw.exit_code != 0 or is_error:
                message = text or result_event.get("subtype") or "qwen reported an error"
                if raw.exit_code != 0 and not text:
                    return nonzero_result(ctx, raw, text=text)
                return error_result(ctx, raw, "NONZERO_EXIT", str(message), text=text)
            if not isinstance(raw_answer, str) or not raw_answer.strip():
                fallback = _last_assistant_text(events)
                if fallback is not None and fallback.strip():
                    return success_result(
                        ctx,
                        raw,
                        fallback,
                        session_id=str(session_id) if session_id else None,
                        cost=_extract_cost(result_event.get("usage")),
                    )
                return error_result(
                    ctx,
                    raw,
                    "PARSE_ERROR",
                    "qwen result event carried no text answer",
                    text=raw.stdout.strip(),
                )
            return success_result(
                ctx,
                raw,
                text,
                session_id=str(session_id) if session_id else None,
                cost=_extract_cost(result_event.get("usage")),
            )

        # No result element: fall back to the last assistant message.
        answer = _last_assistant_text(events)
        if raw.exit_code != 0:
            if answer is not None:
                return success_result(ctx, raw, answer)
            return nonzero_result(ctx, raw)
        if answer is not None:
            return success_result(ctx, raw, answer)

        return error_result(
            ctx,
            raw,
            "PARSE_ERROR",
            "qwen -o json produced no result or assistant message",
            text=raw.stdout.strip(),
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful qwen run must emit a parseable JSON event array."""
        return bool(_parse_events(raw.stdout))


def _parse_events(stdout: str) -> list[dict[str, Any]] | None:
    """Return the list of event objects from a JSON array, or ``None`` if it is not one."""
    candidate = stdout.strip()
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [event for event in parsed if isinstance(event, dict)]


def _last_event_of_type(events: list[dict[str, Any]], etype: str) -> dict[str, Any] | None:
    """Return the last event whose ``type`` equals ``etype``, or ``None``."""
    for event in reversed(events):
        if event.get("type") == etype:
            return event
    return None


def _last_assistant_text(events: list[dict[str, Any]]) -> str | None:
    """Return the text of the last ``assistant`` message's content, or ``None``.

    The assistant event carries ``message.content`` as a list of parts; we join the text of the
    ``{"type":"text","text":...}`` parts.
    """
    assistant = _last_event_of_type(events, "assistant")
    if assistant is None:
        return None
    message = assistant.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if text is not None:
                parts.append(str(text))
    if not parts:
        return None
    return "".join(parts)


def _extract_cost(usage: Any) -> Cost | None:
    """Build a :class:`Cost` from a qwen ``usage`` block, if any tokens are present."""
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return Cost(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
