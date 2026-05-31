# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Kiro adapter (``kiro-cli``).

Invocation: ``kiro-cli chat --no-interactive "<prompt>" [flags]`` with the prompt as a positional
argument right after ``--no-interactive``. Kiro has no working-directory flag, so the spec sets
``cwd`` instead; no system-prompt flag, so the role preamble is folded into the prompt; and no
file-attach flag, so in-scope files are appended to the prompt. Models are selected with
``--model``; a prior session is continued with ``--resume-id``. Safety maps to ``--trust-tools`` /
``--trust-all-tools``.

The chat answer prints as plain Markdown to stdout (there is no JSON mode for the answer), so
``parse_output`` takes the trimmed stdout as the final text and reports no session id. The
``--list-models --format json`` and ``whoami --format json`` subcommands do emit JSON.

Flags verified 2026-05-30 against ``kiro-cli --help`` (kiro-cli 2.5.0).
"""

from __future__ import annotations

import json
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
from .results import nonzero_result, strip_ansi, success_result, timeout_result


class KiroAdapter(BaseCLIAdapter):
    """Adapter for the Kiro CLI (``kiro-cli``)."""

    id = "kiro"
    display_name = "Kiro"
    binary = "kiro-cli"
    static_models = ()
    version_args = ("--version",)

    def check_auth(self) -> AuthStatus:
        """Report auth from ``KIRO_API_KEY`` or, failing that, a persisted ``whoami`` session."""
        return self._auth_from_env_or_command(
            ("KIRO_API_KEY",),
            [self.binary, "whoami", "--format", "json"],
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Kiro's feature flags: resume, model selection, file context, list-models."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=False,
            supports_file_context=True,
            supports_list_models=True,
            supports_system_prompt=False,
            output_mode=OutputMode.TEXT,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Kiro's trust flags, defaulting to read-only file access.

        Kiro gates tool use with ``--trust-tools`` (a comma-separated allowlist) and
        ``--trust-all-tools`` (bypass). read_only and propose trust only ``fs_read``; write adds
        ``fs_write``; yolo trusts all tools. Anything unexpected falls back to read-only.
        """
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["--trust-tools=fs_read,fs_write"], note="trust file read and write")
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--trust-all-tools"], note="trust all tools (bypass)")
        return SafetyFlags(args=["--trust-tools=fs_read"], note="trust file read only")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Pure mapping from request to argv. Never builds a shell string.

        The role preamble is folded into the prompt (no system-prompt flag) and in-scope files are
        appended (no file-attach flag). Kiro has no working-directory flag, so the working dir is
        carried on ``spec.cwd``. Safety args are appended and safety env is overlaid.
        """
        prompt = self._compose_prompt(req.prompt, ctx.role_preamble)
        prompt = self._with_files(prompt, req.files)
        argv = [self.binary, "chat", "--no-interactive", prompt]

        if req.target.model:
            argv += ["--model", req.target.model]
        if req.session_id:
            argv += ["--resume-id", req.session_id]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def available_models(self) -> list[str]:
        """List models via ``kiro-cli chat --list-models --format json``; fall back to the static set.

        Kiro emits ``{"models": [{"model_id": ..., "model_name": ...}], "default_model": ...}``.
        The parser unwraps that ``models`` wrapper and reads each entry's ``model_id``; it also
        tolerates a bare list or a list of strings. Any failure (non-zero exit, bad JSON, an
        unexpected shape) yields the static model set rather than raising.
        """
        result = self._probe.run([self.binary, "chat", "--list-models", "--format", "json"], timeout_s=15.0)
        if result.exit_code != 0:
            return list(self.static_models)
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return list(self.static_models)
        models = _model_names(payload)
        return models if models else list(self.static_models)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope. Never raises.

        The chat answer is Markdown on stdout with ANSI color codes and a ``> `` response marker
        in ``--no-interactive`` mode; ANSI sequences are stripped so the text is clean. Success
        returns the trimmed text with no session id. A timeout maps to ``TIMEOUT`` and a non-zero
        exit to ``NONZERO_EXIT``.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)
        text = strip_ansi(raw.stdout).strip()
        if raw.exit_code != 0:
            return nonzero_result(ctx, raw, text=text)
        return success_result(ctx, raw, text, session_id=None)


def _model_names(payload: Any) -> list[str]:
    """Extract model ids from a ``--list-models`` payload.

    Handles Kiro's ``{"models": [...]}`` wrapper (and similar ``data``/``items`` wrappers), a bare
    list of objects, or a list of strings. For object entries, the first present of
    ``model_id``/``model_name``/``id``/``name``/``modelId`` is used.
    """
    if isinstance(payload, dict):
        for key in ("models", "data", "items"):
            wrapped = payload.get(key)
            if isinstance(wrapped, list):
                payload = wrapped
                break
        else:
            return []
    if not isinstance(payload, list):
        return []
    names: list[str] = []
    for item in payload:
        if isinstance(item, str):
            if item:
                names.append(item)
            continue
        if isinstance(item, dict):
            for key in ("model_id", "model_name", "id", "name", "modelId"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    names.append(value)
                    break
    return names
