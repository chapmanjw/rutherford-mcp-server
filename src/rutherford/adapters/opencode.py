# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The OpenCode adapter.

Invocation: ``opencode run --format json [flags] "<prompt>"`` with the prompt as the last
positional argument. ``--format json`` emits raw JSON events on stdout (logs go to a file unless
``--print-logs`` is set, so stdout stays clean). Flags: ``--dir`` (working directory), ``-m`` (a
``provider/model`` string such as ``anthropic/claude-sonnet-4-6``), and ``--session`` (resume).
OpenCode is bring-your-own-model: there is no system-prompt flag, so the role preamble is prepended
to the prompt and in-scope files are appended to it. Auth is an ``ANTHROPIC_API_KEY`` /
``OPENAI_API_KEY`` env key or a persisted ``opencode auth login`` session.

The ``--format json`` stream emits one JSON event per line: text parts carry the assistant's
answer, a ``step_finish`` event carries token usage and cost, and events carry a ``sessionID``
for resume.

Flags verified 2026-05-30 against ``opencode run --help`` (opencode 1.15.13). Note: this version
has no ``-q``/``--quiet`` flag; ``--format json`` is what keeps stdout machine-readable.
"""

from __future__ import annotations

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
from .parsing import CostSpec, extract_cost, parse_jsonl
from .results import error_result, nonzero_result, success_result, timeout_result

#: Permission payload for read-only / propose: deny edits and shell commands.
_PERMISSION_DENY = '{"edit":"deny","bash":"deny"}'
#: Permission payload for write: allow edits and shell commands.
_PERMISSION_ALLOW = '{"edit":"allow","bash":"allow"}'


class OpenCodeAdapter(BaseCLIAdapter):
    """Adapter for the OpenCode CLI (``opencode``)."""

    id = "opencode"
    display_name = "OpenCode"
    binary = "opencode"
    static_models: tuple[str, ...] = ()

    def check_auth(self) -> AuthStatus:
        """Report auth state from a provider env key, then a persisted login. Never logs in."""
        present = self._env_present("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        result = self._probe.run([self.binary, "auth", "list"], timeout_s=15.0)
        if result.exit_code == 0:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="persisted login detected")
        return AuthStatus(
            state=AuthState.NEEDS_LOGIN,
            detail="no provider API key and no persisted login; run `opencode auth login`",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise OpenCode's feature flags: BYO-model, JSONL output, prompt-style file context."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=True,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
            file_context_style="prompt",
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to OpenCode's permission controls, defaulting to deny.

        Read-only and propose deny edits and shell via the ``OPENCODE_PERMISSION`` env var; write
        allows them via the same var; yolo passes ``--dangerously-skip-permissions``. The deny
        default means an unknown mode never gains write access.
        """
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                env={"OPENCODE_PERMISSION": _PERMISSION_ALLOW},
                note="allow edits and shell commands",
            )
        if mode is SafetyMode.YOLO:
            return SafetyFlags(
                args=["--dangerously-skip-permissions"],
                note="bypass all permission checks",
            )
        return SafetyFlags(
            env={"OPENCODE_PERMISSION": _PERMISSION_DENY},
            note="deny edits and shell commands",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``opencode run`` invocation, feeding the prompt via stdin.

        The role preamble is prepended and in-scope files appended to the prompt (OpenCode has no
        system-prompt or file-attach flag). The composed prompt is passed on STDIN rather than as a
        positional argument: OpenCode installs as a Windows npm shim, so it launches via
        ``cmd.exe /c``, and a newline in a ``cmd.exe`` argument truncates the command at the first
        newline -- which would silently drop everything after the first line of a multi-line prompt
        (for example a stance directive joined to a claim with a blank line). stdin carries the full
        multi-line prompt intact. Safety env is overlaid on the spec env.
        """
        prompt = self._with_files(
            self._compose_prompt(req.prompt, ctx.role_preamble),
            req.files,
        )
        argv = [self.binary, "run", "--format", "json"]

        if req.working_dir:
            argv += ["--dir", req.working_dir]
        if req.target.model:
            argv += ["-m", req.target.model]
        if req.session_id:
            argv += ["--session", req.session_id]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        # The prompt rides on stdin (not a positional argv element) so a multi-line prompt
        # survives the cmd.exe shim launch. OpenCode reads the prompt from stdin.
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir, stdin=prompt)

    def available_models(self) -> list[str]:
        """List models via ``opencode models``; fall back to the static set on any failure."""
        result = self._probe.run([self.binary, "models"], timeout_s=15.0)
        if result.exit_code != 0:
            return list(self.static_models)
        models = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return models or list(self.static_models)

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful opencode run must emit at least one JSONL event (--format json)."""
        return bool(parse_jsonl(raw.stdout))

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the OpenCode NDJSON event stream to the normalized envelope. Never raises."""
        if raw.timed_out:
            return timeout_result(ctx, raw)

        events = parse_jsonl(raw.stdout)
        text = _extract_text(events)
        session_id = _extract_session_id(events)
        cost = _extract_cost(events)

        if not text:
            if raw.exit_code != 0:
                return nonzero_result(ctx, raw)
            return error_result(
                ctx,
                raw,
                "PARSE_ERROR",
                "opencode --format json produced no parseable assistant text",
                text=raw.stdout.strip(),
            )

        if raw.exit_code != 0:
            return error_result(ctx, raw, "NONZERO_EXIT", text, text=text)

        return success_result(
            ctx,
            raw,
            text,
            session_id=session_id,
            cost=cost,
        )


def _extract_text(events: list[dict[str, Any]]) -> str:
    """Collect assistant text from ``type``-bearing text events.

    Events that carry an incremental text part are concatenated. If any text event instead
    carries a full snapshot (later events repeat and extend the same text), the longest single
    snapshot is preferred so duplicated prefixes are not doubled.
    """
    parts: list[str] = []
    for event in events:
        if "text" not in str(event.get("type", "")):
            continue
        part = event.get("part")
        chunk = part.get("text") if isinstance(part, dict) else event.get("text")
        if isinstance(chunk, str) and chunk:
            parts.append(chunk)

    if not parts:
        return ""

    joined = "".join(parts).strip()
    longest = max(parts, key=len).strip()
    # If the final snapshot already contains the concatenation, the events were cumulative
    # snapshots rather than deltas; trust the longest single snapshot to avoid doubling.
    if longest and joined.count(longest) > 1:
        return longest
    return joined


def _extract_session_id(events: list[dict[str, Any]]) -> str | None:
    """Return the first ``sessionID`` seen across the event stream, or ``None``."""
    for event in events:
        session_id = event.get("sessionID")
        if isinstance(session_id, str) and session_id:
            return session_id
        part = event.get("part")
        if isinstance(part, dict):
            nested = part.get("sessionID")
            if isinstance(nested, str) and nested:
                return nested
    return None


#: OpenCode's ``step_finish`` event carries the USD ``cost`` inline and the token counts nested under
#: ``tokens`` (``input`` / ``output``) -- on either the event or its ``part``.
_COST = CostSpec(usd_key="cost", tokens_key="tokens", input_keys=("input",), output_keys=("output",))


def _extract_cost(events: list[dict[str, Any]]) -> Cost | None:
    """Build a :class:`Cost` from the last ``step_finish`` event that carries usage/cost."""
    for event in reversed(events):
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        source = part if isinstance(part, dict) else event
        cost = extract_cost(source, _COST)
        if cost is not None:
            return cost
    return None
