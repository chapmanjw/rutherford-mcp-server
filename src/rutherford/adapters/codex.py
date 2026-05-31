# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Codex CLI adapter (OpenAI's ``codex``).

Invocation: ``codex exec --json --skip-git-repo-check`` with the prompt read from **stdin**
(not argv), because on Windows ``codex`` is an npm shim and passing the prompt as an argument
invites shell-quoting trouble. ``-m`` selects a model, ``-C`` sets the working root,
``-s`` / ``-a`` set the sandbox and approval policy, and ``codex exec resume <id>`` resumes a
prior session. Codex has no system-prompt flag, so the role preamble is folded into the prompt.

The ``--json`` flag emits JSONL events; the final answer is the text of the last
``item.completed`` agent message, the session id is the ``thread.started`` ``thread_id``, and
token usage comes from ``turn.completed``. Auth is ``OPENAI_API_KEY`` / ``CODEX_API_KEY`` or a
persisted ``~/.codex/auth.json`` session.

Flags verified 2026-05-30 against ``codex exec --help`` (codex-cli 0.135.0).
"""

from __future__ import annotations

import json
from pathlib import Path
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


class CodexAdapter(BaseCLIAdapter):
    """Adapter for OpenAI's Codex CLI (``codex``)."""

    id = "codex"
    display_name = "Codex"
    binary = "codex"
    static_models: tuple[str, ...] = ()
    version_args = ("--version",)

    def check_auth(self) -> AuthStatus:
        """Report auth from an API-key env var, then a persisted login, without logging in."""
        present = self._env_present("OPENAI_API_KEY", "CODEX_API_KEY")
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        if (Path.home() / ".codex" / "auth.json").exists():
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="persisted session")
        return AuthStatus(
            state=AuthState.NEEDS_LOGIN,
            detail="run codex login or set OPENAI_API_KEY",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Codex's feature flags (JSONL output, resume, model/dir selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to a Codex sandbox policy, defaulting to read-only.

        ``codex exec`` is already non-interactive and takes no approval-policy flag (``-a`` exists
        only on the interactive ``codex``); the sandbox policy alone controls write access.
        read_only and propose use the read-only sandbox; write uses workspace-write; yolo bypasses
        the sandbox and approvals entirely.
        """
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["-s", "workspace-write"], note="workspace-write sandbox")
        if mode is SafetyMode.YOLO:
            return SafetyFlags(
                args=["--dangerously-bypass-approvals-and-sandbox"],
                note="bypass all approvals and sandboxing",
            )
        return SafetyFlags(args=["-s", "read-only"], note="read-only sandbox")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``codex exec`` invocation, with the composed prompt fed via stdin.

        Pure: returns an argv list and an stdin string, never a shell command line. The prompt
        carries the role preamble (Codex has no system-prompt flag) and any in-scope files.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)

        argv = [self.binary, "exec"]
        if req.session_id:
            argv += ["resume", req.session_id]
        argv += ["--json", "--skip-git-repo-check"]

        if req.working_dir:
            argv += ["-C", req.working_dir]
        if req.target.model:
            argv += ["-m", req.target.model]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(
            argv=argv,
            env=dict(safety.env),
            cwd=req.working_dir,
            stdin=prompt,
        )

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL event stream into the normalized envelope, defensively.

        Walks events line by line: ``thread.started`` gives the session id, the last
        ``item.completed`` agent message gives the answer, ``turn.completed`` gives token usage,
        and ``turn.failed`` / ``error`` (or a non-zero exit) yields a failure. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        events = _parse_events(raw.stdout)

        session_id: str | None = None
        answer: str | None = None
        cost: Cost | None = None
        failure: str | None = None

        for event in events:
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id")
                if thread_id:
                    session_id = str(thread_id)
            elif etype == "item.completed":
                text = _agent_message_text(event.get("item"))
                if text is not None:
                    answer = text
            elif etype == "turn.completed":
                cost = _extract_cost(event.get("usage")) or cost
            elif etype == "turn.failed":
                err = event.get("error")
                if isinstance(err, dict):
                    failure = str(err.get("message") or "codex turn failed")
                else:
                    failure = "codex turn failed"
            elif etype == "error":
                failure = str(event.get("message") or "codex reported an error")

        if raw.exit_code != 0:
            if answer is not None:
                return success_result(ctx, raw, answer, session_id=session_id, cost=cost)
            return nonzero_result(ctx, raw, text=failure or "")

        if failure is not None:
            return error_result(ctx, raw, "NONZERO_EXIT", failure, text=answer or "")

        if answer is None:
            return error_result(
                ctx,
                raw,
                "PARSE_ERROR",
                "codex --json produced no agent message",
                text=raw.stdout.strip(),
            )

        return success_result(ctx, raw, answer, session_id=session_id, cost=cost)


def _parse_events(stdout: str) -> list[dict[str, Any]]:
    """Return the JSON objects from a JSONL stream, skipping blank or unparseable lines."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _agent_message_text(item: Any) -> str | None:
    """Return the text of an agent-message item, handling both event shapes, or ``None``.

    The completed item is either ``{"type":"agent_message","text":...}`` or carries the message
    under ``{"details":{"type":"agent_message","text":...}}``. We accept whichever is present.
    """
    if not isinstance(item, dict):
        return None
    details = item.get("details")
    if isinstance(details, dict) and details.get("type") == "agent_message":
        text = details.get("text")
        return str(text) if text is not None else ""
    if item.get("type") == "agent_message":
        text = item.get("text")
        return str(text) if text is not None else ""
    return None


def _extract_cost(usage: Any) -> Cost | None:
    """Build a :class:`Cost` from a ``turn.completed`` usage block, if any tokens are present."""
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return Cost(input_tokens=input_tokens, output_tokens=output_tokens)
