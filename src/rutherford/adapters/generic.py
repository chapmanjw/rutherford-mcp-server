# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The config-driven generic adapter.

A well-behaved CLI -- one with a clean headless invocation and deterministic stdout (plain text or
a single JSON object) -- can be added as a :class:`~rutherford.config.schema.GenericAdapterConfig`
entry instead of a code module. This adapter assembles the argv from that config (binary, base
args, safety fragments, model flag, working-dir flag, extra args, then the prompt as the final
positional argument or on stdin) and extracts the answer from text or a dotted JSON path. CLIs
whose output needs custom parsing (streaming events, transcript files) still need a code adapter.
"""

from __future__ import annotations

import json
from typing import Any

from ..config.schema import GenericAdapterConfig
from ..domain.enums import AuthState, OutputMode, SafetyMode
from ..domain.error_codes import ErrorCode
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
from .results import error_result, nonzero_result, success_result, timeout_result


class GenericAdapter(BaseCLIAdapter):
    """An adapter whose entire behavior is driven by a :class:`GenericAdapterConfig`."""

    def __init__(self, config: GenericAdapterConfig, *, probe: CommandProbe | None = None) -> None:
        super().__init__(probe)
        self.id = config.id
        self.display_name = config.display_name
        self.binary = config.binary
        self.static_models = tuple(config.static_models)
        self.version_args = tuple(config.version_args)
        self._config = config

    def check_auth(self) -> AuthStatus:
        if self._config.auth_env:
            return self._auth_from_env_or_command(tuple(self._config.auth_env))
        return AuthStatus(state=AuthState.UNKNOWN, detail="no auth_env configured for this generic adapter")

    def capabilities(self) -> AdapterCapabilities:
        config = self._config
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=config.model_flag is not None,
            supports_working_dir=config.working_dir_flag is not None,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=config.output_mode,
            runtime=config.runtime,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        safety = self._config.safety
        args = {
            SafetyMode.READ_ONLY: safety.read_only,
            SafetyMode.PROPOSE: safety.propose,
            SafetyMode.WRITE: safety.write,
            SafetyMode.YOLO: safety.yolo,
        }[mode]
        return SafetyFlags(args=list(args), note=f"{self.id}: {mode.value}")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        config = self._config
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)

        argv = [self.binary, *config.base_args]
        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        if config.model_flag and req.target.model:
            argv += [config.model_flag, req.target.model]
        if config.working_dir_flag and req.working_dir:
            argv += [config.working_dir_flag, req.working_dir]
        argv += list(config.extra_args)

        stdin: str | None = None
        if config.prompt_on_stdin:
            stdin = prompt
        else:
            argv.append(prompt)

        return InvocationSpec(
            argv=argv,
            env=dict(safety.env),
            cwd=req.working_dir,
            runtime=config.runtime,
            stdin=stdin,
        )

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        if raw.timed_out:
            return timeout_result(ctx, raw)
        if raw.exit_code not in (0, None):
            return nonzero_result(ctx, raw)

        text = self._extract_text(raw.stdout)
        if text is None:
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                f"could not extract text from {self.id} output (output_mode={self._config.output_mode.value})",
                text=raw.stdout.strip(),
            )
        return success_result(ctx, raw, text)

    def _extract_text(self, stdout: str) -> str | None:
        """Extract the final answer per the configured output mode."""
        if self._config.output_mode is OutputMode.JSON:
            payload = _last_json_object(stdout)
            if payload is None:
                return None
            if self._config.json_text_path:
                value = _dotted_get(payload, self._config.json_text_path)
                if value is None or isinstance(value, (dict, list, bool)):
                    return None
                return str(value)
            return json.dumps(payload, ensure_ascii=False)
        # TEXT, JSONL, and TRANSCRIPT generic CLIs return their stdout verbatim.
        return stdout.strip()


def _last_json_object(stdout: str) -> dict[str, Any] | None:
    """Return the last line of ``stdout`` (or the whole text) that parses as a JSON object."""
    for candidate in (*reversed(stdout.splitlines()), stdout):
        text = candidate.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _dotted_get(payload: dict[str, Any], path: str) -> Any:
    """Follow a dotted key path (e.g. ``message.content``) into nested dicts."""
    current: Any = payload
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current
