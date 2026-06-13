# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The OpenHands adapter (``openhands``).

Invocation: ``openhands --headless --json -t "<task>"``. ``--headless`` runs without the interactive
UI (and auto-approves actions), ``--json`` streams JSONL events, and ``-t`` carries the task.
``openhands`` is a native ``.exe`` launched directly, so the multi-line task rides as the ``-t`` value
without a ``cmd.exe`` newline hazard.

A required env overlay: ``PYTHONIOENCODING=utf-8`` (plus ``PYTHONUTF8=1`` and
``OPENHANDS_SUPPRESS_BANNER=1``). Without UTF-8 stdio, OpenHands crashes with a ``UnicodeEncodeError``
when it prints a checkmark / wave glyph to a Windows ``cp1252`` pipe -- it never reaches the model.
With the overlay it runs and emits its JSONL ``MessageEvent`` stream interleaved with Rich UI text; the
non-JSON lines are simply skipped by the JSONL parser. The answer is the last
``{"source":"agent","kind":"MessageEvent","llm_message":{"content":[{"type":"text","text":"..."}]}}``
event's text; the trailing ``... --resume <uuid> ...`` hint line gives the resumable session id.

SAFETY CAVEAT (read_only is best-effort). ``--headless`` auto-approves actions and OpenHands has no
read-only mode, so ``read_only`` / ``propose`` add ``--llm-approve`` (an LLM security analyzer that
gates high-risk actions) as a best-effort restriction, while ``write`` / ``yolo`` add ``--always-approve``
(approve everything). ``write_uses_bypass`` is True (write == the approve-all bypass); ``verify_read_only``
is the post-hoc backstop. Auth is a configured LLM (OpenHands Cloud login or a stored LLM key) with no
non-interactive check, so ``check_auth`` reports ``unknown`` and ``doctor`` confirms it live.

Flags verified 2026-06-13 against ``openhands --help`` (OpenHands CLI 1.16.0, SDK 1.21.0).
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.enums import AuthState, OutputMode, SafetyMode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    DelegationRequest,
    DelegationResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)
from .base import BaseCLIAdapter
from .parsing import finalize_answer, parse_jsonl
from .results import timeout_result

#: Env overlay that forces UTF-8 stdio so OpenHands does not crash printing glyphs to a cp1252 pipe,
#: plus banner suppression to keep the JSONL stream clean.
_UTF8_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "OPENHANDS_SUPPRESS_BANNER": "1"}
#: The resume hint OpenHands prints near the end (``... --resume <id> ...``), used as the resumable
#: session handle. The hint carries the dashed UUID that ``--resume`` accepts, unlike the dashless form
#: on the adjacent ``Conversation ID:`` line.
_CONV_ID_RE = re.compile(r"--resume\s+([0-9a-fA-F-]{8,})")


class OpenHandsAdapter(BaseCLIAdapter):
    """Adapter for the OpenHands CLI (``openhands``)."""

    id = "openhands"
    display_name = "OpenHands"
    binary = "openhands"
    static_models: tuple[str, ...] = ()
    #: OpenHands runs a configured LLM (any litellm provider, e.g. OpenRouter), so the vendor depends on
    #: that configuration; provenance infers it from the model id when one is known.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Report ``unknown``: OpenHands has no non-interactive auth check, so ``doctor`` verifies it live.

        The LLM credential lives in a stored OpenHands config (or an OpenHands Cloud session) with no
        cheap ``whoami``, so reporting ``unknown`` (not a guessed ``needs_login``) lets ``doctor``'s live
        round trip decide -- the same posture as Antigravity and Continue.
        """
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="OpenHands has no non-interactive auth check; doctor verifies it with a live round trip",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise OpenHands's feature flags (JSONL stream, resume; LLM picked by config not a flag)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=False,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to OpenHands's approval posture, failing CLOSED.

        ``--headless`` auto-approves and OpenHands has no read-only mode, so ``read_only`` / ``propose``
        add ``--llm-approve`` (an LLM analyzer gates high-risk actions -- a best-effort restriction,
        ``verify_read_only`` is the backstop), while ``write`` / ``yolo`` add ``--always-approve``
        (approve everything; ``write_uses_bypass`` is True). An unknown mode falls through to
        ``--llm-approve`` -- never to approve-all.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--always-approve"], note="approve all actions without confirmation")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=["--always-approve"],
                note="approve all actions (OpenHands has no posture between llm-approve and approve-all)",
            )
        if mode is SafetyMode.PROPOSE:
            return SafetyFlags(
                args=["--llm-approve"], note="LLM security analyzer gates high-risk actions (best-effort)"
            )
        return SafetyFlags(
            args=["--llm-approve"],
            note="best-effort: --headless auto-approves; the LLM analyzer gates high-risk actions (fail-closed)",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``openhands --headless --json`` invocation. Pure; argv list only, never a shell string.

        The role preamble and in-scope files are folded into the task (no system-prompt flag), passed as
        the ``-t`` value. The UTF-8 env overlay is always applied so the run does not crash on Windows.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "--headless", "--json"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        if req.session_id:
            argv += ["--resume", req.session_id]
        argv += list(ctx.extra_args)
        argv += ["-t", prompt]

        return InvocationSpec(argv=argv, env={**_UTF8_ENV, **safety.env}, cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL ``MessageEvent`` stream into the normalized envelope, defensively.

        The answer is the last agent ``MessageEvent``'s text; the ``Conversation ID`` line is the session
        id; an error event (or a non-zero exit) is a failure. Rich UI lines are skipped. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        answer: str | None = None
        failure: str | None = None

        for event in parse_jsonl(raw.stdout):
            kind = event.get("kind")
            if kind == "MessageEvent" and event.get("source") == "agent":
                message = event.get("llm_message")
                if isinstance(message, dict):
                    text = _message_text(message.get("content"))
                    if text is not None:
                        answer = text.strip()
            elif kind in ("AgentErrorEvent", "ErrorEvent") or event.get("source") == "error":
                failure = str(event.get("error") or event.get("message") or "openhands reported an error")

        match = _CONV_ID_RE.search(raw.stdout)
        session_id = match.group(1) if match else None

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            failure=failure,
            no_output_message="openhands --json produced no agent message",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful openhands run must emit at least one JSONL event (``--json``)."""
        return bool(parse_jsonl(raw.stdout))


def _message_text(content: Any) -> str | None:
    """Join the ``text`` parts of an OpenHands ``llm_message`` content list, or ``None`` if not a list."""
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    ]
    return "".join(parts)
