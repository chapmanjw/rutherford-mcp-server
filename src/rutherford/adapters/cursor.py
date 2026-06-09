# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Cursor adapter (``cursor-agent``).

Invocation: ``cursor-agent -p --output-format json --trust`` with the prompt read from
**stdin** (not argv), because on Windows ``cursor-agent`` is a shim and passing the prompt as
an argument invites shell-quoting trouble. ``--trust`` is required in headless print mode --
without it Cursor prompts for workspace trust and hangs. ``--workspace`` sets the working root
(Cursor has no ``--add-dir``), ``--model`` selects a model (free plans accept only the id
``auto``; named models require a paid plan, so nothing is hardcoded), and ``--resume <id>``
resumes a prior session. Cursor has no system-prompt flag, so the role preamble is folded into
the prompt.

The ``--output-format json`` flag prints a single JSON object: ``result`` is the answer text,
``session_id`` resumes, ``is_error`` / ``subtype`` signal success or an in-band error, and
``usage`` carries token counts. Auth is ``CURSOR_API_KEY`` or a persisted login reported by
``cursor-agent status`` (exit 0 = logged in).

Flags verified 2026-05-30 against ``cursor-agent --help`` (cursor-agent 2026.05.28).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
from .parsing import CostSpec, JsonEnvelopeParser


class CursorAdapter(BaseCLIAdapter):
    """Adapter for Cursor's headless agent CLI (``cursor-agent``)."""

    id = "cursor"
    display_name = "Cursor"
    binary = "cursor-agent"
    static_models: tuple[str, ...] = ()
    version_args = ("--version",)

    def check_auth(self) -> AuthStatus:
        """Report auth from ``CURSOR_API_KEY`` or a persisted login, without logging in."""
        return self._auth_from_env_or_command(("CURSOR_API_KEY",), [self.binary, "status"])

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Cursor's feature flags (JSON output, resume, model/workspace selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=True,
            supports_system_prompt=False,
            output_mode=OutputMode.JSON,
            file_context_style="workspace",
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to a Cursor mode/force flag, defaulting conservatively.

        Cursor's ``-p`` print mode has all tools (including write and shell) enabled by default,
        so read-only and propose must pin a restrictive mode. read_only uses ``--mode ask``
        (Q&A, no edits); propose uses ``--mode plan`` (analyze and propose, no edits); write
        keeps the default print behavior (edit access); yolo uses ``--force`` to run everything.
        """
        if mode is SafetyMode.READ_ONLY:
            return SafetyFlags(args=["--mode", "ask"], note="ask mode: Q&A, read-only")
        if mode is SafetyMode.PROPOSE:
            return SafetyFlags(args=["--mode", "plan"], note="plan mode: analyze and propose, no edits")
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--force"], note="force: run everything without approval")
        return SafetyFlags(args=[], note="default print mode: edit access")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``cursor-agent`` invocation, with the composed prompt fed via stdin.

        Pure: returns an argv list and an stdin string, never a shell command line. ``--trust``
        is always present so headless mode does not block on a workspace-trust prompt. The
        prompt carries the role preamble (Cursor has no system-prompt flag) and any in-scope
        files.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)

        argv = [self.binary, "-p", "--output-format", "json", "--trust"]

        if req.working_dir:
            argv += ["--workspace", req.working_dir]
        if req.target.model:
            argv += ["--model", req.target.model]
        if req.session_id:
            argv += ["--resume", req.session_id]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(
            argv=argv,
            env=dict(safety.env),
            cwd=req.working_dir,
            stdin=prompt,
        )

    def available_models(self) -> list[str]:
        """List models via ``cursor-agent --list-models``, falling back to the static set.

        Output is lines like ``auto - Auto`` and ``gpt-5.2 - GPT-5.2`` around a header line
        (``Available models``) and a trailing ``Tip:`` line. The id is the text before the
        first `` - `` on each line that contains it; header/tip/blank lines are skipped. Any
        failure returns the static set rather than raising.
        """
        try:
            result = self._probe.run([self.binary, "--list-models"], timeout_s=15.0)
        except Exception:
            return list(self.static_models)
        if result.exit_code != 0:
            return list(self.static_models)

        models: list[str] = []
        for line in result.stdout.splitlines():
            candidate = line.strip()
            if " - " not in candidate:
                continue
            model_id = candidate.split(" - ", 1)[0].strip()
            if model_id:
                models.append(model_id)
        return models or list(self.static_models)

    def fallback_model(self) -> str | None:
        """``auto`` is available on every Cursor plan, so it is the safe retry when a named model
        is rejected (for example on a free plan)."""
        return "auto"

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the single JSON object into the normalized envelope, defensively.

        Reads the last JSON object in stdout. ``result`` is the answer, ``session_id`` resumes,
        and a failure is signalled by ``is_error`` true, a non-success ``subtype``, or a non-zero
        exit. ``usage`` gives token counts. Never raises.
        """
        return _PARSER.parse(raw, ctx)

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful cursor run must carry a JSON result object (--output-format json)."""
        return _PARSER.contract_ok(raw)


def _is_error(payload: Mapping[str, Any]) -> bool:
    """Cursor signals an error via ``is_error`` true or any ``subtype`` other than ``success``."""
    subtype = str(payload.get("subtype", ""))
    return bool(payload.get("is_error")) or (subtype != "" and subtype != "success")


#: The shared envelope parser configured for Cursor: the answer is ``result``, the cost is the token
#: counts under ``usage`` (``inputTokens``/``outputTokens``), and Cursor reports no USD figure.
_PARSER = JsonEnvelopeParser(
    cli_name="cursor-agent",
    is_error=_is_error,
    cost=CostSpec(tokens_key="usage", input_keys=("inputTokens",), output_keys=("outputTokens",)),
    no_object_message="cursor-agent --output-format json produced no parseable JSON object",
    no_text_message="cursor-agent reported success but the JSON object had no `result` text",
)
