# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Junie adapter (JetBrains' ``junie``) -- the stdin-pipe-required case.

Invocation: ``junie --input-format text --output-format json --skip-update-check`` with the prompt fed
on **stdin**. Junie's binary requires a real stdin handle: launched with stdin detached (``DEVNULL``)
it aborts immediately with ``Junie failed with the message: Incorrect function`` (a Windows console
I/O error). Rutherford's runner connects a stdin pipe exactly when an :class:`InvocationSpec` carries
``stdin``, so the adapter always sets it -- the prompt doubles as the required pipe (``--input-format
text`` reads the task from stdin). ``--skip-update-check`` keeps the run from staging a self-update
mid-call. ``--project <dir>`` sets the working root, ``--model`` / ``--effort`` select the model and
reasoning tier, and ``--resume --session-id <id>`` follows up a prior session.

``--output-format json`` prints one object: ``{"sessionId":"...","result":"<answer>","changes":[...],
"llmUsage":[{"model":...,"cost":...,"inputTokens":...,"outputTokens":...}, ...]}``. Junie drives
several models internally (a primary plus helper models), so cost is the **sum** across ``llmUsage``
rather than a single token block -- a bespoke read, hence a hand-written ``parse_output`` rather than
the shared envelope parser. The ``result`` may be a short Markdown report (Junie's own formatting), not
a bare answer.

SAFETY CAVEAT (read_only is best-effort). Junie is an autonomous coding agent with no headless
read-only / plan flag (``--brave`` is interactive-only), so ``read_only`` / ``propose`` are best-effort
and ``write`` / ``yolo`` cannot escalate (``write_uses_bypass`` is True); ``verify_read_only`` is the
post-hoc backstop. Auth is a persisted JetBrains token (``junie.jetbrains.com/cli``) or a BYOK provider
key under ``~/.junie``, with no non-interactive check -- ``check_auth`` reports ``unknown`` and
``doctor`` confirms it live. Junie is slow (tens of seconds per call); a generous
``[adapters.junie] timeout_s`` is recommended.

Flags verified 2026-06-13 against ``junie --help`` (Junie 26.6.8, build 1892.26).
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import AuthState, Effort, OutputMode, SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    Cost,
    DelegationRequest,
    DelegationResult,
    EffortFlags,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)
from .base import BaseCLIAdapter
from .parsing import last_json_object, str_field
from .results import error_result, nonzero_result, success_result, timeout_result


class JunieAdapter(BaseCLIAdapter):
    """Adapter for JetBrains' Junie CLI (``junie``)."""

    id = "junie"
    display_name = "Junie"
    binary = "junie"
    static_models: tuple[str, ...] = ()
    #: Junie runs on a JetBrains backend or a BYOK provider (``--provider openai|anthropic|...``), so
    #: the vendor depends on the model; provenance infers it from the resolved model id.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Report ``unknown``: Junie has no non-interactive auth check, so ``doctor`` verifies it live.

        Auth is a persisted JetBrains CLI token or a BYOK provider key under ``~/.junie`` with no cheap
        ``whoami``; reporting ``unknown`` (not a guessed ``needs_login``) lets ``doctor``'s live round
        trip be the trustworthy signal, the same posture as Antigravity and Continue.
        """
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="Junie has no non-interactive auth check; doctor verifies it with a live round trip",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Junie's feature flags (JSON object, resume, model/effort/project selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSON,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Every SafetyMode maps to Junie's single headless posture (read_only best-effort).

        Junie exposes no headless read-only / plan / bypass flag (``--brave`` is interactive-only), so no
        flag is added for any mode: ``read_only`` / ``propose`` are best-effort (``verify_read_only`` is
        the post-hoc backstop) and ``write`` / ``yolo`` cannot escalate. The note records the constraint.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=[], note="junie has no headless bypass flag (--brave is interactive-only)")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=[], note="junie has no headless write posture distinct from its default run")
        return SafetyFlags(
            args=[],
            note="best-effort: junie has no headless read-only flag; verify_read_only is the post-hoc backstop",
        )

    def map_effort(self, effort: Effort) -> EffortFlags:
        """Map effort to Junie's ``--effort low|medium|high`` flag (F8a, 2-L-map).

        Junie's reasoning effort tops out at ``high``, so ``xhigh`` clamps to ``high`` (recorded in the
        note and ``effort_applied``); ``low`` / ``medium`` / ``high`` pass through.
        """
        applied = self._clamp_effort(effort, Effort.HIGH)
        note = f"--effort {applied.value}"
        if applied is not effort:
            note += f" (clamped from {effort.value})"
        return EffortFlags(args=["--effort", applied.value], note=note, applied=applied)

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``junie`` invocation with the prompt on stdin. Pure; argv list only.

        The composed prompt (role preamble + task + in-scope files, since Junie has no system-prompt
        flag) rides on stdin -- which also satisfies Junie's hard requirement for a real stdin handle.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "--input-format", "text", "--output-format", "json", "--skip-update-check"]
        if req.working_dir:
            argv += ["--project", req.working_dir]
        if req.target.model:
            argv += ["--model", req.target.model]
        if req.session_id:
            argv += ["--resume", "--session-id", req.session_id]
        if ctx.effort is not None:
            argv += self.map_effort(ctx.effort).args
        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir, stdin=prompt)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the single JSON object into the normalized envelope, defensively.

        ``result`` is the answer, ``sessionId`` resumes, and cost is the sum across the ``llmUsage``
        per-model entries. A non-zero exit or an empty ``result`` is a failure. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        payload = last_json_object(raw.stdout)
        if payload is None:
            if raw.exit_code != 0:
                return nonzero_result(ctx, raw)
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                "junie --output-format json produced no parseable JSON object",
                text=raw.stdout.strip(),
            )

        text = str_field(payload, "result")
        session_value = payload.get("sessionId")
        session_id = str(session_value) if session_value else None
        if raw.exit_code != 0:
            return error_result(
                ctx,
                raw,
                ErrorCode.NONZERO_EXIT,
                text or f"junie exited with code {raw.exit_code}",
                text=text,
                session_id=session_id,
            )
        if not text.strip():
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                "junie reported success but the JSON object had no `result` text",
                text=raw.stdout.strip(),
                session_id=session_id,
            )

        return success_result(ctx, raw, text, session_id=session_id, cost=_sum_usage(payload.get("llmUsage")))

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful junie run must carry a JSON object (``--output-format json``)."""
        return last_json_object(raw.stdout) is not None


def _sum_usage(usage: Any) -> Cost | None:
    """Sum Junie's per-model ``llmUsage`` entries into one :class:`Cost`, or ``None`` when absent.

    Junie drives several models per task and reports cost/tokens per model, so the run's cost is the
    sum across the list. A missing or non-numeric figure is skipped; an empty/absent list yields
    ``None`` (no cost data) rather than a zeroed Cost.
    """
    if not isinstance(usage, list):
        return None
    usd = 0.0
    input_tokens = 0
    output_tokens = 0
    seen = False
    for entry in usage:
        if not isinstance(entry, dict):
            continue
        for key, add in (("cost", "usd"), ("inputTokens", "in"), ("outputTokens", "out")):
            value = entry.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                seen = True
                if add == "usd":
                    usd += float(value)
                elif add == "in":
                    input_tokens += int(value)
                else:
                    output_tokens += int(value)
    if not seen:
        return None
    return Cost(
        usd=usd or None,
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
    )
