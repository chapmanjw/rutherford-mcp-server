# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The pi adapter (``pi``) -- the read-only-by-toolset case.

Invocation: ``pi -p --mode json [--tools read,grep,find,ls] "<prompt>"`` with the composed prompt as
the trailing **positional** message. ``-p`` is non-interactive; ``--mode json`` emits a JSONL event
stream; ``--model <provider/id>`` selects a model, ``--system-prompt`` carries the role preamble,
``--thinking`` the reasoning effort, and ``--session-id`` resumes a session. The npm shim is launched
through PowerShell (see :func:`~rutherford.runtime.launch.prepare_argv`), so the multi-line positional
prompt is not truncated by ``cmd.exe``.

pi streams every reasoning/text delta, so the answer is reassembled from the **last** assistant
``message_end`` event's ``text`` content parts (``thinking`` parts are dropped); the first ``session``
event's ``id`` is the session handle, and the assistant message's ``usage.cost.total`` gives the USD
cost.

SAFETY (genuine read-only via the tool allowlist). pi's built-in tools are read, bash, edit, write,
grep, find, ls. ``read_only`` / ``propose`` pass ``--tools read,grep,find,ls`` so no edit/write/bash
tool exists for the run -- a genuine read-only sandbox; ``write`` / ``yolo`` leave the default toolset
(which includes edit/write/bash and auto-runs in ``-p`` mode), so ``write_uses_bypass`` is True (pi has
no posture between full tools and none). Auth is a configured provider key (pi lists the provider's
models with ``pi --list-models``); that command's success is the cheap signal, with ``doctor`` as the
live backstop.

Flags verified 2026-06-13 against ``pi --help`` (pi 0.79.3).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..domain.enums import Effort, OutputMode, SafetyMode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    Cost,
    DelegationRequest,
    DelegationResult,
    EffortFlags,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)
from .base import BaseCLIAdapter
from .parsing import finalize_answer, parse_jsonl
from .results import timeout_result

#: The read-only tool allowlist: read + the three off-by-default read tools, with no edit/write/bash.
_READONLY_TOOLS = "read,grep,find,ls"


class PiAdapter(BaseCLIAdapter):
    """Adapter for the pi coding agent CLI (``pi``)."""

    id = "pi"
    display_name = "pi"
    binary = "pi"
    static_models: tuple[str, ...] = ()
    #: pi is bring-your-own-model across many providers (default ``google``), so the vendor depends on
    #: the configured provider/model; provenance infers it from the resolved model id.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``pi --list-models`` (a configured provider lists its models), never logging in.

        pi reads a provider key from the environment or its config; listing models succeeds only when a
        provider is configured. That exit status is the cheap signal, with ``doctor`` as the live backstop.
        """
        return self._auth_from_env_or_command((), [self.binary, "--list-models"])

    def available_models(self) -> list[str]:
        """List models via ``pi --list-models``, falling back to the static set.

        Output is a table: a header row (``provider  model  context ...``) then one row per model. The
        model id is the second whitespace-delimited column. Any failure returns the static set.
        """
        result = self._probe.run([self.binary, "--list-models"], timeout_s=15.0)
        if result.exit_code != 0:
            return list(self.static_models)
        models: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0] == "provider":
                continue
            models.append(fields[1])
        return models or list(self.static_models)

    def capabilities(self) -> AdapterCapabilities:
        """Advertise pi's feature flags (JSONL stream, resume, model/system/effort/list-models)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=True,
            supports_system_prompt=True,
            output_mode=OutputMode.JSONL,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to pi's tool allowlist, failing CLOSED.

        ``read_only`` / ``propose`` restrict the run to ``--tools read,grep,find,ls`` (no edit/write/bash
        -- a genuine read-only sandbox); ``write`` / ``yolo`` leave the default toolset, which includes
        edit/write/bash and auto-runs in ``-p`` mode (so ``write_uses_bypass`` is True). An unknown mode
        falls through to the read-only allowlist.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=[], note="default toolset: read/bash/edit/write all enabled")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=[], note="default toolset: read/bash/edit/write (pi has no posture between full and read-only)"
            )
        if mode is SafetyMode.PROPOSE:
            return SafetyFlags(args=["--tools", _READONLY_TOOLS], note="read-only tools: no edit/write/bash")
        return SafetyFlags(
            args=["--tools", _READONLY_TOOLS], note="read-only tools: no edit/write/bash (fail-closed default)"
        )

    def map_effort(self, effort: Effort) -> EffortFlags:
        """Map effort to pi's ``--thinking`` flag (F8a, 2-L-cov); pi supports every tier including xhigh."""
        return EffortFlags(args=["--thinking", effort.value], note=f"--thinking {effort.value}", applied=effort)

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``pi -p --mode json`` invocation. Pure; argv list only, never a shell string.

        The role preamble rides on ``--system-prompt``, in-scope files are folded into the prompt, and the
        composed prompt is the trailing positional message.
        """
        prompt = self._with_files(req.prompt, req.files)
        argv = [self.binary, "-p", "--mode", "json"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        if req.target.model:
            argv += ["--model", req.target.model]
        if ctx.role_preamble:
            argv += ["--system-prompt", ctx.role_preamble]
        if ctx.effort is not None:
            argv += self.map_effort(ctx.effort).args
        if req.session_id:
            argv += ["--session-id", req.session_id]
        argv += list(ctx.extra_args)
        argv.append(prompt)

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL event stream into the normalized envelope, defensively.

        The answer is the last assistant ``message_end`` event's text content; the session id is the
        ``session`` event's ``id``; cost is the assistant message's ``usage.cost.total``. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        answer: str | None = None
        session_id: str | None = None
        cost: Cost | None = None
        failure: str | None = None

        for event in parse_jsonl(raw.stdout):
            etype = event.get("type")
            if etype == "session":
                sid = event.get("id")
                if sid and session_id is None:
                    session_id = str(sid)
            elif etype == "message_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = _assistant_text(message.get("content"))
                    if text is not None:
                        answer = text
                    cost = _pi_cost(message.get("usage")) or cost
            elif etype == "error":
                failure = str(event.get("message") or event.get("error") or "pi reported an error")

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            cost=cost,
            failure=failure,
            no_output_message="pi --mode json produced no assistant message",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful pi run must emit at least one JSONL event (``--mode json``)."""
        return bool(parse_jsonl(raw.stdout))


def _assistant_text(content: Any) -> str | None:
    """Join the ``text`` content parts of an assistant message (dropping ``thinking``), or ``None``.

    Returns ``None`` only when ``content`` is not a list at all (a malformed message); a list with no
    text parts yields the empty string (the message was produced but carried only reasoning).
    """
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    ]
    return "".join(parts)


def _pi_cost(usage: Any) -> Cost | None:
    """Build a :class:`Cost` from pi's ``usage`` block (USD nested at ``cost.total``), or ``None``."""
    if not isinstance(usage, dict):
        return None
    cost_block = usage.get("cost")
    usd = cost_block.get("total") if isinstance(cost_block, dict) else None
    input_tokens = usage.get("input")
    output_tokens = usage.get("output")
    total_tokens = usage.get("totalTokens")
    if usd is None and input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    try:
        return Cost(usd=usd, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
    except ValidationError:
        return None
