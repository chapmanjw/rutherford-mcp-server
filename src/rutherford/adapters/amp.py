# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Amp adapter (Sourcegraph's ``amp``).

Invocation: ``amp -x "<prompt>" --stream-json``. ``-x`` / ``--execute`` is the non-interactive mode
(it runs the prompt and prints only the agent's final message), and ``--stream-json`` makes that a
Claude-Code-compatible JSONL event stream. ``amp`` is a native ``.exe`` launched directly, so the
multi-line prompt rides as the ``-x`` value without a ``cmd.exe`` newline hazard. The role preamble
and any in-scope files are folded into the prompt (Amp has no system-prompt flag).

The stream's terminal ``{"type":"result","subtype":"success","is_error":false,"result":"<answer>",
"session_id":"T-..."}`` event carries the answer and verdict; token usage is on the ``assistant``
message's ``usage`` block. Amp serves Anthropic Claude models (the mode picks the model, not a
``--model`` flag), so ``supports_model_selection`` is False and provenance reports ``anthropic``.

SAFETY CAVEAT (read_only is best-effort; no per-call lever). Amp's only permission switches live in its
settings file (``amp.dangerouslyAllowAll`` to bypass confirmations, ``amp.tools.disable`` / ``amp.permissions``
to restrict), which a *pure* ``build_invocation`` cannot write. ``-x`` execute mode **auto-runs its tools
(including edits) without confirmation** -- verified live that a read_only delegation applied a file edit --
so ``read_only`` / ``propose`` are **best-effort, not a guaranteed sandbox** (the Antigravity case), and
``write`` / ``yolo`` carry no distinct posture (``write_uses_bypass`` is True). The ``verify_read_only``
git guard is the post-hoc backstop. Auth is ``AMP_API_KEY`` or a persisted ``amp login`` session, checked
cheaply with ``amp usage``.

Flags verified 2026-06-13 against ``amp --help`` (Amp CLI, build 2026-06-13).
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import OutputMode, SafetyMode
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
from .parsing import CostSpec, extract_cost, finalize_answer, parse_jsonl, str_field
from .results import timeout_result


class AmpAdapter(BaseCLIAdapter):
    """Adapter for Sourcegraph's Amp CLI (``amp``)."""

    id = "amp"
    display_name = "Amp"
    binary = "amp"
    static_models: tuple[str, ...] = ()
    #: Amp serves Anthropic Claude; the mode (deep/rush/smart) picks the model, not a selector flag.
    provider = "anthropic"

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``AMP_API_KEY`` or a persisted login (``amp usage`` exit 0), never logging in."""
        return self._auth_from_env_or_command(("AMP_API_KEY",), [self.binary, "usage"])

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Amp's feature flags (JSONL stream; no model selector, no per-call resume flag)."""
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=False,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Every SafetyMode maps to Amp's single execute-mode posture (read_only best-effort).

        Amp's permission switches are settings-file values, not per-call flags, so no flag is added for
        any mode. ``-x`` execute mode auto-runs its tools (including edits) without confirmation -- verified
        live that read_only applied a file edit -- so ``read_only`` / ``propose`` are **best-effort** (the
        Antigravity case; ``verify_read_only`` is the post-hoc backstop) and ``write`` / ``yolo`` carry no
        distinct posture. The note records the constraint.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(
                args=[], note="amp's allow-all (amp.dangerouslyAllowAll) is a settings value, not a per-call flag"
            )
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=[], note="execute mode auto-runs tools -- no posture distinct from the best-effort default"
            )
        return SafetyFlags(
            args=[],
            note="best-effort: execute mode auto-runs its tools (including edits); amp exposes no per-call "
            "read-only flag; verify_read_only is the post-hoc backstop",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``amp -x --stream-json`` invocation. Pure; argv list only, never a shell string."""
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        safety = self.map_safety(ctx.safety_mode)
        argv = [self.binary, "-x", prompt, "--stream-json", *safety.args, *ctx.extra_args]
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL stream into the normalized envelope, defensively.

        The terminal ``result`` event gives the answer (or an ``is_error`` failure) and the session id;
        the ``assistant`` message's ``usage`` block gives token cost. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        answer: str | None = None
        session_id: str | None = None
        failure: str | None = None
        cost: Cost | None = None

        for event in parse_jsonl(raw.stdout):
            etype = event.get("type")
            if etype == "assistant":
                message = event.get("message")
                if isinstance(message, dict):
                    cost = extract_cost(message.get("usage"), _COST) or cost
            elif etype == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if sid and session_id is None:
                    session_id = str(sid)
            elif etype == "result":
                sid = event.get("session_id")
                if sid:
                    session_id = str(sid)
                if event.get("is_error") or _nonsuccess_subtype(event):
                    failure = str_field(event, "result") or str(event.get("subtype")) or "amp reported an error"
                else:
                    result = event.get("result")
                    if isinstance(result, str):
                        answer = result

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            cost=cost,
            failure=failure,
            no_output_message="amp --stream-json produced no result event",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful amp run must emit at least one JSONL event (``--stream-json``)."""
        return bool(parse_jsonl(raw.stdout))


def _nonsuccess_subtype(event: dict[str, Any]) -> bool:
    """Whether a ``result`` event's ``subtype`` signals failure (anything other than ``success``)."""
    subtype = str(event.get("subtype", ""))
    return subtype != "" and subtype != "success"


#: Amp's assistant ``usage`` block carries token counts (``input_tokens`` / ``output_tokens``) with no
#: USD figure -- the default cost spec keyed on those names.
_COST = CostSpec(input_keys=("input_tokens",), output_keys=("output_tokens",))
