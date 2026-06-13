# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Continue CLI adapter (``cn``).

Invocation: ``cn -p --readonly --silent "<prompt>"`` with the composed prompt as the trailing
**positional** argument. ``-p`` (print) is the headless mode; ``--silent`` strips ``<think>`` blocks
and excess whitespace, leaving just the final answer text; ``--readonly`` is the read-only posture;
``--model <owner/pkg>`` selects a Continue Hub model; ``--rule <text>`` injects a rule (used here to
carry the role preamble, since ``cn`` has no system-prompt flag). The prompt rides as a positional
rather than on stdin because ``cn`` does not read a *programmatic* stdin pipe reliably; the npm shim is
launched through PowerShell on Windows (see :func:`~rutherford.runtime.launch.prepare_argv`), so a
multi-line role preamble is preserved.

The answer is parsed as **plain text** rather than ``--format json``: Continue's JSON mode is not a
stable envelope -- it wraps a non-JSON model answer as ``{"response": "...", "status": "success"}`` but
passes a model answer that is itself valid JSON straight through (observed ``{"result": 42}`` for a
numeric reply), so the ``response`` key is unreliable. ``-p --silent`` yields a clean text answer in
both cases. Auth is a persisted ``cn login`` session with no non-interactive check, so ``check_auth``
reports ``unknown`` and ``doctor`` confirms it with a live round trip.

Flags verified 2026-06-13 against ``cn --help`` (Continue CLI 1.5.45).
"""

from __future__ import annotations

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
from .parsing import TextParser


class ContinueAdapter(BaseCLIAdapter):
    """Adapter for Continue's headless CLI (``cn``)."""

    id = "cn"
    display_name = "Continue"
    binary = "cn"
    static_models: tuple[str, ...] = ()
    #: Continue is bring-your-own-model (Hub slugs for Anthropic / OpenAI / others through one CLI),
    #: so the vendor depends on the chosen model; provenance infers it from the id.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Report ``unknown``: Continue has no non-interactive auth check, so ``doctor`` verifies it live.

        Auth is a persisted ``cn login`` session under ``~/.continue`` (or a configured provider key),
        with no cheap, portable ``whoami``. A false "needs login" would wrongly bench a working CLI, so
        this reports ``unknown`` and lets ``doctor``'s live round trip be the trustworthy signal --
        the same posture as Antigravity and Qwen.
        """
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="Continue has no non-interactive auth check; doctor verifies it with a live round trip",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Continue's feature flags (plain-text answer, model selection, rule-as-system-prompt)."""
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=True,
            output_mode=OutputMode.TEXT,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Continue's ``--readonly`` / ``--auto`` posture, failing CLOSED.

        Continue has two headless postures: ``--readonly`` (plan mode -- read-only tools only) and
        ``--auto`` (all tools allowed). It has nothing between them, so ``write`` and ``yolo`` both map
        to ``--auto`` (``write_uses_bypass`` is True), while ``read_only`` and ``propose`` map to
        ``--readonly``. An unknown future mode falls through to ``--readonly`` -- never to ``--auto``.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--auto"], note="auto mode: all tools allowed")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=["--auto"], note="auto mode: all tools (Continue has no posture between read-only and auto)"
            )
        if mode is SafetyMode.PROPOSE:
            return SafetyFlags(args=["--readonly"], note="plan mode: read-only tools, propose without applying")
        return SafetyFlags(args=["--readonly"], note="plan mode: read-only tools (fail-closed default)")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``cn -p --silent`` invocation. Pure; argv list only, never a shell string.

        The role preamble rides on ``--rule`` (Continue's nearest system-prompt mechanism), the in-scope
        file list is folded into the prompt, and the composed prompt is the trailing positional.
        """
        prompt = self._with_files(req.prompt, req.files)
        argv = [self.binary, "-p", "--silent"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        if req.target.model:
            argv += ["--model", req.target.model]
        if ctx.role_preamble:
            argv += ["--rule", ctx.role_preamble]
        argv += list(ctx.extra_args)
        argv.append(prompt)

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        return _PARSER.parse(raw, ctx)


#: Continue ``-p --silent`` prints just the final answer text, so the plain-text parser (strip ANSI +
#: trim) applies. A clean exit with no text is a parse error -- a model should always produce an answer.
_PARSER = TextParser(empty_message="cn produced no output")
