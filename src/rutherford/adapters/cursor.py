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

from ..domain.enums import EFFORT_ORDER, Effort, OutputMode, SafetyMode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    DelegationRequest,
    DelegationResult,
    EffortFlags,
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
        """Map every SafetyMode to a Cursor mode/force flag, failing CLOSED on anything unknown.

        Cursor's ``-p`` print mode has all tools (including write and shell) enabled by default,
        so the permissive postures must be EXPLICIT and the catch-all restrictive: read_only uses
        ``--mode ask`` (Q&A, no edits); propose uses ``--mode plan`` (analyze and propose, no
        edits); write keeps the default print behavior (edit access); yolo uses ``--force`` to run
        everything. A SafetyMode this adapter does not know (a future, likely more-restrictive
        value) falls through to ``--mode ask`` -- never to Cursor's edit-capable default.
        """
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=[], note="default print mode: edit access")
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--force"], note="force: run everything without approval")
        if mode is SafetyMode.PROPOSE:
            return SafetyFlags(args=["--mode", "plan"], note="plan mode: analyze and propose, no edits")
        return SafetyFlags(args=["--mode", "ask"], note="ask mode: Q&A, read-only (fail-closed default)")

    def map_effort(self, effort: Effort) -> EffortFlags:
        """Cursor selects reasoning effort via the MODEL ID, not a flag (F8a, 2-L-cov).

        Cursor encodes the tier in the model id as a plain ``-<tier>`` suffix (``gpt-5.2-high``,
        ``claude-opus-4-8-high``), so there is no free-standing flag to add --
        :meth:`build_invocation` rewrites ``--model`` to carry the suffix instead. Tops out at
        ``high`` (``xhigh`` clamps). Reported here as ``applied`` for the run record; the actual
        rewrite (and its ``auto`` / already-tiered guards) is in the build path, which knows the model.

        Suffix convention confirmed against ``cursor-agent --list-models`` on this machine
        (2026-06-13): every family exposes ``<model>-<tier>`` (e.g. ``gpt-5.2-high``,
        ``claude-opus-4-8-high``), while ``-thinking-<tier>`` is a *separate* extended-thinking axis
        that exists only for the Claude families -- so a plain ``-<tier>`` is the correct, cross-family
        effort suffix. A live round trip of a named tier was not possible (this account is auto-only:
        every named model returns MODEL_UNAVAILABLE), so a model whose family has no tiered variant
        relies on Cursor's ``auto`` model-fallback to recover rather than failing.
        """
        applied = self._clamp_effort(effort, Effort.HIGH)
        note = f"reasoning effort via the model-id '-{applied.value}' suffix"
        if applied is not effort:
            note += f" (clamped from {effort.value})"
        return EffortFlags(note=note, applied=applied)

    @staticmethod
    def _model_with_effort(model: str, effort: Effort) -> str:
        """Append Cursor's ``-<tier>`` reasoning suffix to a bare model id, or leave it unchanged.

        Left unchanged for ``auto`` (the universal fallback, which has no tiered variant) and for any
        model that already encodes an effort or serving choice -- a ``thinking`` segment, a trailing
        ``-fast`` serving variant (a ``-fast`` model already names its tier, e.g. ``gpt-5.2-high-fast``,
        or has none, e.g. ``composer-2.5-fast``, where appending a tier *after* ``-fast`` would only
        invent an invalid id), or a trailing ``-<tier>`` -- so an explicit user choice is respected and
        an effort request never double-suffixes a valid id into one Cursor would reject. Tops out at
        ``high`` (``xhigh`` clamps). A bare model whose family has no ``-<tier>`` variant produces an
        unknown id that Cursor's ``auto`` model-fallback then recovers (see :meth:`map_effort`).
        """
        lowered = model.lower()
        if model == "auto" or "thinking" in lowered or lowered.endswith("-fast"):
            return model
        if any(lowered.endswith(f"-{tier.value}") for tier in EFFORT_ORDER):
            return model  # already carries an explicit reasoning tier -- respect the user's choice
        return f"{model}-{BaseCLIAdapter._clamp_effort(effort, Effort.HIGH).value}"

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``cursor-agent`` invocation, with the composed prompt fed via stdin.

        Pure: returns an argv list and an stdin string, never a shell command line. ``--trust``
        is always present so headless mode does not block on a workspace-trust prompt. The
        prompt carries the role preamble (Cursor has no system-prompt flag) and any in-scope
        files. A reasoning ``effort`` is folded into the ``--model`` id (Cursor's convention).
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)

        argv = [self.binary, "-p", "--output-format", "json", "--trust"]

        if req.working_dir:
            argv += ["--workspace", req.working_dir]
        if req.target.model:
            model = self._model_with_effort(req.target.model, ctx.effort) if ctx.effort else req.target.model
            argv += ["--model", model]
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
        result = self._probe.run([self.binary, "--list-models"], timeout_s=15.0)
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
