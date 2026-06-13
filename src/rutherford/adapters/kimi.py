# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Kimi Code adapter (``kimi``) -- Moonshot's ``kimi-code``, not the legacy Kimi CLI.

Invocation: ``kimi -p "<prompt>" --output-format stream-json``. ``-p`` runs one prompt
non-interactively and prints the response; ``--output-format stream-json`` emits a small JSONL stream
whose answer is the last ``{"role":"assistant","content":"<text>"}`` line and whose session handle is a
trailing ``{"role":"meta","type":"session.resume_hint","session_id":"..."}`` line. ``-m`` selects a
model alias from ``~/.kimi-code/config.toml``; ``-S <id>`` resumes a session. ``kimi`` is a native
``.exe`` launched directly, so the multi-line prompt rides as the ``-p`` value without a ``cmd.exe``
newline hazard.

SAFETY CAVEAT (one fixed headless posture). The permission-mode flags ``--plan`` / ``--auto`` / ``-y``
are interactive-mode switches and do **not** combine with ``-p`` (``kimi -p --plan`` is rejected with
"Cannot combine --prompt with --plan"), so headless prompt mode has a single fixed posture with no
read-only / write / yolo lever. Every SafetyMode therefore maps to no extra flag: ``read_only`` /
``propose`` are **best-effort** (verified live that read_only applied a file edit -- the Antigravity
case; ``verify_read_only`` is the post-hoc backstop), and ``write`` / ``yolo`` cannot escalate beyond
that posture (``write_uses_bypass`` is True).

Auth is a ``kimi login`` device-code session or a configured provider (``kimi provider list``); that
command's exit status is the cheap signal, with ``doctor``'s live round trip as the backstop.

Flags verified 2026-06-13 against ``kimi --help`` (Kimi Code 0.14.2).
"""

from __future__ import annotations

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
from .parsing import finalize_answer, parse_jsonl
from .results import timeout_result


class KimiAdapter(BaseCLIAdapter):
    """Adapter for Moonshot's Kimi Code CLI (``kimi``)."""

    id = "kimi"
    display_name = "Kimi Code"
    binary = "kimi"
    static_models: tuple[str, ...] = ()
    #: Moonshot's CLI; the default models are Moonshot/Kimi, but a provider can be reconfigured, so
    #: this is the best-guess vendor, not a confirmed one (a recognized model id overrides it).
    provider = "moonshot"

    def check_auth(self) -> AuthStatus:
        """Resolve auth from a configured provider (``kimi provider list``) or a known env key, no login.

        Auth is a ``kimi login`` device-code session or a provider configured in
        ``~/.kimi-code/config.toml`` (listed by ``kimi provider list``). The env keys are checked first
        for an explicitly-keyed provider; otherwise the command's exit status is the signal and
        ``doctor`` is the live backstop.
        """
        return self._auth_from_env_or_command(("KIMI_API_KEY", "MOONSHOT_API_KEY"), [self.binary, "provider", "list"])

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Kimi Code's feature flags (JSONL stream, resume, model selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Every SafetyMode maps to Kimi's single headless posture (read_only best-effort).

        ``-p`` (prompt mode) rejects the interactive permission flags ``--plan`` / ``--auto`` / ``-y``,
        so there is no per-call lever: no flag is added for any mode. ``read_only`` / ``propose`` are
        best-effort (``verify_read_only`` is the post-hoc backstop) and ``write`` / ``yolo`` cannot
        escalate. The note records the constraint so ``doctor`` says it out loud.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(
                args=[], note="kimi -p has one fixed posture; --yolo is interactive-only and not combinable"
            )
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=[], note="kimi -p has one fixed posture; it cannot escalate to a distinct write mode"
            )
        return SafetyFlags(
            args=[],
            note="best-effort: kimi -p has one fixed posture (interactive --plan is rejected with -p); "
            "verify_read_only is the post-hoc backstop",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``kimi -p`` invocation. Pure; argv list only, never a shell string.

        The role preamble and in-scope files are folded into the prompt (no system-prompt flag), which
        is the ``-p`` value. ``kimi`` is a native ``.exe``, so the multi-line value is preserved.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "-p", prompt, "--output-format", "stream-json"]
        if req.target.model:
            argv += ["-m", req.target.model]
        if req.session_id:
            argv += ["-S", req.session_id]
        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL stream into the normalized envelope, defensively.

        The answer is the last ``{"role":"assistant","content":"<text>"}`` line; the session id is the
        ``session.resume_hint`` meta line; an ``{"role":"error",...}`` line (or a non-zero exit) is a
        failure. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        answer: str | None = None
        session_id: str | None = None
        failure: str | None = None
        cost: Cost | None = None

        for event in parse_jsonl(raw.stdout):
            role = event.get("role")
            if role == "assistant":
                content = event.get("content")
                if isinstance(content, str):
                    answer = content
            elif role == "meta" and event.get("type") == "session.resume_hint":
                sid = event.get("session_id")
                if sid:
                    session_id = str(sid)
            elif role == "error":
                failure = str(event.get("content") or event.get("message") or "kimi reported an error")

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            cost=cost,
            failure=failure,
            no_output_message="kimi --output-format stream-json produced no assistant message",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful kimi run must emit at least one JSONL event (``--output-format stream-json``)."""
        return bool(parse_jsonl(raw.stdout))
