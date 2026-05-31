# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Goose adapter (Block's ``goose`` CLI).

Invocation: ``goose run -q [--no-session | -n <name> -r] [--system <preamble>]
[--model <model>] -t "<prompt>"``. Goose is bring-your-own-model: a provider and model are
configured out of band (``GOOSE_PROVIDER`` plus a provider API key, or ``goose configure``),
so there is no static model list. It has no working-dir flag, so the working directory is set
on the spec's ``cwd`` instead. Approval posture is set via the ``GOOSE_MODE`` environment
variable (``smart_approve`` vs ``auto``), not a flag.

Output: plain text on stdout. Goose's ``--output-format json`` schema is unstable, so this
adapter does not request it and reads the answer straight from stdout. Headless ``goose run``
does not surface a resumable session id in its text output, so ``session_id`` is left ``None``.

Flags verified 2026-05-30 against ``goose run --help`` (goose CLI 1.x).
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
from .results import nonzero_result, strip_ansi, success_result, timeout_result


class GooseAdapter(BaseCLIAdapter):
    """Adapter for Block's Goose CLI (``goose``)."""

    id = "goose"
    display_name = "Goose"
    binary = "goose"
    static_models: tuple[str, ...] = ()

    #: Provider API-key env vars any one of which, together with GOOSE_PROVIDER, signals auth.
    _PROVIDER_KEYS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")

    def check_auth(self) -> AuthStatus:
        """Probe auth without triggering a login.

        Authenticated when ``GOOSE_PROVIDER`` is set together with any provider API key; else a
        configured install is inferred from a zero exit of ``goose info -v``; else ``UNKNOWN``.
        """
        if self._env_present("GOOSE_PROVIDER") and self._env_present(*self._PROVIDER_KEYS):
            return AuthStatus(
                state=AuthState.AUTHENTICATED,
                detail="GOOSE_PROVIDER and a provider API key are set",
            )
        result = self._probe.run([self.binary, "info", "-v"], timeout_s=15.0)
        if result.exit_code == 0:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="goose is configured")
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="set GOOSE_PROVIDER and a provider key, or run goose configure",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Goose's feature flags."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=False,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=True,
            output_mode=OutputMode.TEXT,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to a ``GOOSE_MODE`` value.

        Goose has no approval flag; the posture is set via the ``GOOSE_MODE`` env var.
        Read-only and propose run with ``smart_approve`` (no unattended edits); write and yolo
        run with ``auto``. The conservative default for any unrecognized mode is
        ``smart_approve``.
        """
        if mode in (SafetyMode.WRITE, SafetyMode.YOLO):
            return SafetyFlags(env={"GOOSE_MODE": "auto"}, note="auto-approve actions")
        return SafetyFlags(env={"GOOSE_MODE": "smart_approve"}, note="approve only safe actions")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Pure mapping from request to invocation; never builds a shell string.

        Goose has no working-dir flag, so the directory is set on ``spec.cwd``. The role preamble
        uses the native ``--system`` flag, so it is not also prepended to the prompt; files are
        folded into the prompt via ``_with_files``. A named session resumes with ``-n <id> -r``;
        with no session id a one-shot ``--no-session`` run is used. Safety env is overlaid last.
        """
        prompt = self._with_files(req.prompt, req.files)
        argv = [self.binary, "run", "-q"]

        if req.target.model:
            argv += ["--model", req.target.model]
        if ctx.role_preamble:
            argv += ["--system", ctx.role_preamble]

        if req.session_id:
            argv += ["-n", req.session_id, "-r"]
        else:
            argv += ["--no-session"]

        argv += ["-t", prompt]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Map the raw process result to the normalized envelope; never raises.

        Timeout maps to ``TIMEOUT``; a non-zero exit maps to ``NONZERO_EXIT`` (surfacing
        stderr). On a zero exit the trimmed stdout is the answer -- including the empty string,
        which is still treated as success. Goose does not surface a resumable session id in
        headless text output, so ``session_id`` is ``None``.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)
        if raw.exit_code != 0:
            return nonzero_result(ctx, raw)
        return success_result(ctx, raw, strip_ansi(raw.stdout).strip(), session_id=None)
