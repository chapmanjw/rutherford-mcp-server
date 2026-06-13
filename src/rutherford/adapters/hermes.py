# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Hermes Agent adapter (``hermes``) -- the plain-text one-shot case.

Invocation: ``hermes -z "<prompt>"``. ``-z`` / ``--oneshot`` is Hermes's scripting mode: it sends a
single prompt and prints **only the final response text** to stdout (no banner, spinner, tool
previews, or session line), then exits -- so the answer is plain text, parsed with the shared
:class:`~rutherford.adapters.parsing.TextParser`. ``-m provider/model`` overrides the model for this
run (``anthropic/claude-sonnet-4.6`` style); the role preamble and any in-scope files are folded into
the prompt, which is passed as the ``-z`` value (``hermes`` is a native ``.exe`` launched directly, so
a multi-line argument is preserved -- no ``cmd.exe`` newline hazard).

SAFETY CAVEAT (read_only is best-effort). One-shot mode states that *approvals are auto-bypassed*: the
agent loads its full toolset (read, edit, write, shell) and runs without prompting, because a
non-interactive script has no one to approve. Hermes exposes no per-call read-only / plan flag, so
``read_only`` / ``propose`` are **best-effort, not guaranteed** -- an agent that chooses to edit will
mutate the workspace. This is the Antigravity pattern: ``write_uses_bypass`` is True (one-shot has no
write posture distinct from its auto-bypass, so ``write`` == the default one-shot run), ``yolo`` adds
``--yolo`` (bypass even *dangerous*-command approval), and the optional ``verify_read_only`` git guard
is the post-hoc ``READONLY_VIOLATED`` backstop. Restricting the toolset to read-only tools via ``-t``
is a future tightening once a confirmed read-only toolset name is pinned.

Auth is a pooled-credentials / ``.env`` setup under ``~/.hermes`` (Nous Portal, or a provider key),
listed by ``hermes auth list``; that command's exit status is the cheap signal, with ``doctor``'s live
round trip as the backstop. The one-shot stream carries no machine-readable session id, so resume is
not surfaced (``supports_resume`` is False).

Flags verified 2026-06-13 against ``hermes --help`` (Hermes Agent 0.16.0).
"""

from __future__ import annotations

from ..domain.enums import OutputMode, SafetyMode
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


class HermesAdapter(BaseCLIAdapter):
    """Adapter for the Hermes Agent CLI (``hermes``)."""

    id = "hermes"
    display_name = "Hermes Agent"
    binary = "hermes"
    static_models: tuple[str, ...] = ()
    #: Bring-your-own-model (``provider/model``, e.g. ``anthropic/...`` / ``openrouter/...``), so the
    #: vendor depends on the chosen model; provenance infers it from the id.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``hermes auth list`` (its exit status), never logging in.

        Hermes keeps pooled provider credentials and a ``.env`` under ``~/.hermes``; ``hermes auth list``
        succeeds when that store is set up. There is no single env var to key on (the credential may be a
        Nous device-code OAuth, a copilot token, or a provider key), so the command's exit code is the
        cheap signal and ``doctor`` is the live backstop.
        """
        return self._auth_from_env_or_command((), [self.binary, "auth", "list"])

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Hermes's feature flags (plain-text one-shot, model selection, no resume surfaced)."""
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.TEXT,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Hermes's one-shot posture (read_only best-effort), failing CLOSED.

        ``-z`` already auto-bypasses normal approvals, so ``read_only`` / ``propose`` / ``write`` carry
        no extra flag -- they are the same one-shot run, and ``read_only`` / ``propose`` are best-effort
        (Hermes has no flag that prevents an edit; ``verify_read_only`` is the post-hoc backstop).
        ``yolo`` adds ``--yolo`` to bypass even dangerous-command approval. An unknown mode falls through
        to the no-extra-flag best-effort posture -- never to ``--yolo``.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--yolo"], note="bypass even dangerous-command approval prompts")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=[],
                note="one-shot auto-bypasses approvals -- write == the default one-shot run (no distinct posture)",
            )
        return SafetyFlags(
            args=[],
            note="best-effort: one-shot auto-bypasses approvals and Hermes has no read-only flag; "
            "verify_read_only is the post-hoc backstop",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``hermes -z`` one-shot invocation. Pure; argv list only, never a shell string.

        The composed prompt (role preamble + task + in-scope files, since Hermes has no system-prompt
        flag) is the ``-z`` value. ``hermes`` is a native ``.exe``, so the multi-line value is preserved.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "-z", prompt]
        if req.target.model:
            argv += ["-m", req.target.model]
        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        return _PARSER.parse(raw, ctx)


#: Hermes one-shot prints just the final answer text, so the plain-text parser (strip ANSI + trim)
#: applies. A clean exit with no text is a parse error -- a model should always produce an answer.
_PARSER = TextParser(empty_message="hermes -z produced no output")
