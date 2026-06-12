# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Factory Droid adapter (``droid``).

Invocation: ``droid exec --output-format json`` with the composed prompt on **stdin** (Droid's
``exec`` accepts the prompt as a positional, ``-f <file>``, or stdin; stdin keeps a multi-line prompt
-- role preamble + task + file list -- off argv, away from the Windows command-line length cap and the
cmd.exe first-newline truncation an npm-shim install would hit). ``-m`` selects a model, ``--cwd`` sets
the working directory, ``-s <id>`` resumes a session, and the safety flags below escalate write access.
Droid has an ``--append-system-prompt`` flag, but the role preamble is multi-line, so it is folded into
the stdin prompt instead (the Claude Code precedent), and ``supports_system_prompt`` is False.

``--output-format json`` prints one JSON object carrying the answer in ``result``, a resumable
``session_id``, an ``is_error`` / ``subtype`` verdict, and a nested ``usage`` block
(``input_tokens`` / ``output_tokens`` / cache counts). The shape is the Claude Code envelope family, so
it reuses the shared :class:`JsonEnvelopeParser`. The verified v0.144.2 ``result`` object carries no
``total_cost_usd`` (the USD figure is absent for at least some models), so ``cost`` is populated from the
token block and ``cost.usd`` stays ``None`` unless a future build adds the field.

Auth is ``FACTORY_API_KEY`` / ``FACTORY_TOKEN`` or a persisted ``droid`` login under ``~/.factory``
(``auth.v2.file``). The browser OAuth flow fires only at first login, never at call time.

Flags and the JSON envelope verified 2026-06-11 against docs.factory.ai (droid-exec / cli-reference) and
``@factory/cli`` v0.144.x; the JSON field names mirror the Claude Code ``--output-format json`` envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
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
from ..runtime.probe import CommandProbe
from .base import BaseCLIAdapter
from .parsing import CostSpec, JsonEnvelopeParser


class DroidAdapter(BaseCLIAdapter):
    """Adapter for Factory's Droid CLI (``droid``)."""

    id = "droid"
    display_name = "Droid (Factory)"
    binary = "droid"
    static_models: tuple[str, ...] = ()
    #: Droid is bring-your-own-model (Anthropic / OpenAI / Gemini / GLM / Kimi and custom endpoints
    #: through one binary), so the vendor depends on the chosen model; provenance infers it from the id.
    provider = None

    def __init__(self, probe: CommandProbe | None = None, *, data_root: Path | None = None) -> None:
        super().__init__(probe)
        #: Where a persisted ``droid`` login lives. Injectable so the auth probe is unit-testable
        #: against a temp dir, mirroring the Antigravity adapter.
        self._data_root = data_root if data_root is not None else Path.home() / ".factory"

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``FACTORY_API_KEY`` / ``FACTORY_TOKEN``, then a persisted login, never logging in.

        The OAuth credential itself lives in the system keyring (with a file fallback), which cannot be
        verified cheaply or portably, so a persisted ``~/.factory/auth.v2.file`` is the on-disk "has been
        set up" marker. A keyring-only install with no env key and no such file reads as ``NEEDS_LOGIN``
        (the safe direction, never a false positive); ``doctor``'s live check is the backstop.
        """
        present = self._env_present("FACTORY_API_KEY", "FACTORY_TOKEN")
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        if (self._data_root / "auth.v2.file").exists():
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="persisted Factory session detected")
        return AuthStatus(
            state=AuthState.NEEDS_LOGIN,
            detail="set FACTORY_API_KEY/FACTORY_TOKEN or run `droid` and log in",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Droid's feature flags (JSON envelope, resume, model/dir selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSON,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Droid's autonomy flags, defaulting to read-only.

        Bare ``droid exec`` is read-only (reads, diffs, status). ``--auto low|medium|high`` escalates
        write capability; ``--skip-permissions-unsafe`` is the full bypass (incompatible with ``--auto``).
        ``write`` deliberately uses the lowest ``--auto low`` tier so Rutherford does not silently grant
        package installs, commits, or pushes; ``medium`` / ``high`` are reachable only via per-adapter
        ``extra_args``. No bypass is ever the default.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--skip-permissions-unsafe"], note="bypass all guardrails")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["--auto", "low"], note="auto low: create/edit files, non-destructive commands")
        return SafetyFlags(args=[], note="default read-only: reads, diffs, status only")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``droid exec`` invocation. Pure; argv list only, never a shell string.

        The role preamble (no system-prompt flag is used) and the in-scope file list are folded into the
        prompt, which rides on stdin to dodge the Windows argv newline/length hazards.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "exec", "--output-format", "json"]

        if req.target.model:
            argv += ["-m", req.target.model]
        if req.working_dir:
            argv += ["--cwd", req.working_dir]
        if req.session_id:
            argv += ["-s", req.session_id]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir, stdin=prompt)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        return _PARSER.parse(raw, ctx)

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful droid run must carry a JSON result object (``--output-format json``)."""
        return _PARSER.contract_ok(raw)


def _is_error(payload: Mapping[str, Any]) -> bool:
    """Droid signals an error via ``is_error`` true or an ``error``-prefixed ``subtype``."""
    return bool(payload.get("is_error")) or str(payload.get("subtype", "")).startswith("error")


#: The shared envelope parser configured for Droid: the answer is ``result`` and token counts come from
#: a nested ``usage`` block. ``total_cost_usd`` is kept as the USD key so the figure is captured if a
#: build ever emits it, but it is absent in the verified v0.144.2 output, so cost is tokens-only there.
_PARSER = JsonEnvelopeParser(
    cli_name="droid",
    is_error=_is_error,
    cost=CostSpec(usd_key="total_cost_usd", tokens_key="usage"),
    no_object_message="droid --output-format json produced no parseable JSON object",
    no_text_message="droid reported success but the JSON object had no `result` text",
)
