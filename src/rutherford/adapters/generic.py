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
    Provenance,
    SafetyFlags,
)
from ..runtime.probe import CommandProbe
from .base import BaseCLIAdapter
from .parsing import as_text, dotted_get, last_json_object
from .provenance import infer_provider_from_model
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

    def provenance(self, ctx: InvocationContext) -> Provenance:
        """Use the configured ``provider`` (confirmed) when the config declares one, else the model
        heuristic -- Rutherford cannot otherwise know what an arbitrary configured CLI talks to."""
        if self._config.provider:
            return Provenance(provider=self._config.provider, model=ctx.target.model, confirmed=True)
        return Provenance(provider=infer_provider_from_model(ctx.target.model), model=ctx.target.model, confirmed=False)

    def _extract_text(self, stdout: str) -> str | None:
        """Extract the final answer per the configured output mode.

        For a JSON CLI the answer is the last top-level JSON object (via the robust
        :func:`~rutherford.io.jsontext.last_json_object` scanner, which tolerates prose around the
        object and skips a trailing array), then the scalar leaf at the configured
        ``json_text_path`` -- which schema validation guarantees is set for JSON mode, so there is
        no whole-object fallback here. A non-scalar leaf (object/list/bool) or a missing path
        yields ``None`` -- reported as a parse failure rather than a stringified container. TEXT
        generic CLIs return stdout verbatim.
        """
        if self._config.output_mode is OutputMode.JSON:
            payload = last_json_object(stdout)
            if payload is None:
                return None
            return as_text(dotted_get(payload, self._config.json_text_path or ""))
        # TEXT generic CLIs return their stdout verbatim (JSONL/TRANSCRIPT are rejected at load).
        return stdout.strip()
