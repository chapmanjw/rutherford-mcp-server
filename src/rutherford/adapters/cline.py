# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Cline adapter (``cline``).

Invocation: ``cline --json [--plan] "<prompt>"`` with the composed prompt as the trailing
**positional** argument. ``--json`` emits a JSONL event stream; ``--plan`` is the read-only plan mode;
``-m`` selects a model, ``-P`` a provider, ``-c`` the working directory, ``-s`` a system prompt, and
``--thinking`` the reasoning effort. The npm shim is launched through PowerShell (see
:func:`~rutherford.runtime.launch.prepare_argv`), so the multi-line positional prompt survives -- the
``cmd.exe`` newline truncation that would eat a multi-line role preamble does not apply.

The stream ends with a ``{"type":"run_result","finishReason":"completed","text":"<answer>","usage":
{"inputTokens":...,"outputTokens":...,"totalCost":...},"model":{"id":...,"provider":...}}`` event: the
answer is ``text``, success is ``finishReason == "completed"``, and the ``usage`` block gives cost. The
``hook_event``'s ``taskId`` is surfaced as the session id for the run record, but resume is **not**
advertised: cline's ``--id`` resume mode does not accept a headless follow-up prompt (verified live --
with ``--id`` set, both a positional prompt and piped stdin are rejected), so ``supports_resume`` is False.

SAFETY (genuine read-only via plan mode + no auto-approval). ``cline`` defaults to *act* mode with
``--auto-approve true`` (it edits without prompting). Plan mode ALONE is not enough -- verified live that
``--plan`` with the default ``--auto-approve true`` still applies an edit -- so ``read_only`` / ``propose``
force ``--plan --auto-approve false`` (verified live to leave the file untouched), while ``write`` /
``yolo`` use act mode with ``--auto-approve true`` (Cline has no posture between plan and full
auto-approve, so ``write_uses_bypass`` is True). Auth is a configured provider (``cline auth``) with no
non-interactive check, so ``check_auth`` reports ``unknown`` and ``doctor`` confirms it live.

Flags verified 2026-06-13 against ``cline --help`` (Cline CLI 3.0.24).
"""

from __future__ import annotations

from ..domain.enums import AuthState, Effort, OutputMode, SafetyMode
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
from .parsing import CostSpec, extract_cost, finalize_answer, parse_jsonl
from .results import timeout_result


class ClineAdapter(BaseCLIAdapter):
    """Adapter for the Cline CLI (``cline``)."""

    id = "cline"
    display_name = "Cline"
    binary = "cline"
    static_models: tuple[str, ...] = ()
    #: Cline is bring-your-own-provider (its ``cline`` hosted backend, OpenAI, Anthropic, and more),
    #: so the vendor depends on the configured provider/model; provenance infers it from the model id.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Report ``unknown``: Cline's config/auth commands require a TTY, so ``doctor`` verifies it live.

        ``cline config`` / ``cline auth`` both refuse to run without a terminal, and there is no env-key
        marker for the default ``cline`` provider, so no cheap non-interactive probe is trustworthy.
        Reporting ``unknown`` (not a guessed ``needs_login``) lets ``doctor``'s live round trip decide.
        """
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="Cline has no non-interactive auth check (config/auth need a TTY); doctor verifies it live",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Cline's feature flags (JSONL stream, model/system/effort selection; no resume)."""
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=True,
            output_mode=OutputMode.JSONL,
            write_uses_bypass=True,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Cline's plan / act posture, failing CLOSED.

        Cline's default (act mode + ``--auto-approve true``) edits without prompting, and plan mode ALONE
        does not stop it (verified live), so ``read_only`` / ``propose`` force ``--plan --auto-approve
        false`` (read-only, no tool execution), and ``write`` / ``yolo`` use act mode with
        ``--auto-approve true``. An unknown future mode falls through to the read-only branch -- never to
        the edit-capable default.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--auto-approve", "true"], note="act mode, auto-approve all tools")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=["--auto-approve", "true"],
                note="act mode, auto-approve all tools (Cline has no posture between plan and auto-approve)",
            )
        if mode is SafetyMode.PROPOSE:
            return SafetyFlags(
                args=["--plan", "--auto-approve", "false"], note="plan mode + no auto-approval: read-only, propose only"
            )
        return SafetyFlags(
            args=["--plan", "--auto-approve", "false"],
            note="plan mode + no auto-approval: read-only (fail-closed default)",
        )

    def map_effort(self, effort: Effort) -> EffortFlags:
        """Map effort to Cline's ``--thinking`` flag (F8a, 2-L-cov); Cline supports every tier including xhigh."""
        return EffortFlags(args=["--thinking", effort.value], note=f"--thinking {effort.value}", applied=effort)

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``cline --json`` invocation. Pure; argv list only, never a shell string.

        The role preamble rides on ``-s`` (system prompt), in-scope files are folded into the prompt, and
        the composed prompt is the trailing positional. The safety posture is always explicit.
        """
        prompt = self._with_files(req.prompt, req.files)
        argv = [self.binary, "--json"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        if req.working_dir:
            argv += ["-c", req.working_dir]
        if req.target.model:
            argv += ["-m", req.target.model]
        if ctx.role_preamble:
            argv += ["-s", ctx.role_preamble]
        if ctx.effort is not None:
            argv += self.map_effort(ctx.effort).args
        # No --id: cline's resume mode rejects a headless follow-up prompt (supports_resume is False).
        argv += list(ctx.extra_args)
        argv.append(prompt)

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL event stream into the normalized envelope, defensively.

        The ``run_result`` event gives the answer (``text``), the verdict (``finishReason``), and cost
        (``usage``); a ``hook_event``'s ``taskId`` is the session id; an ``error`` event is a failure.
        Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        answer: str | None = None
        session_id: str | None = None
        failure: str | None = None
        cost: Cost | None = None

        for event in parse_jsonl(raw.stdout):
            etype = event.get("type")
            if etype == "hook_event":
                task_id = event.get("taskId")
                if task_id and session_id is None:
                    session_id = str(task_id)
            elif etype == "run_result":
                text = event.get("text")
                if isinstance(text, str):
                    answer = text
                cost = extract_cost(event.get("usage") or event.get("aggregateUsage"), _COST) or cost
                reason = str(event.get("finishReason", ""))
                if reason and reason != "completed":
                    failure = f"cline finished with reason: {reason}"
            elif etype == "error":
                failure = str(event.get("message") or "cline reported an error")

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            cost=cost,
            failure=failure,
            no_output_message="cline --json produced no run_result event",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful cline run must emit at least one JSONL event (``--json``)."""
        return bool(parse_jsonl(raw.stdout))


#: Cline's ``usage`` block carries token counts (``inputTokens`` / ``outputTokens``) and a USD figure
#: (``totalCost``) directly on the block (no nested token key).
_COST = CostSpec(usd_key="totalCost", input_keys=("inputTokens",), output_keys=("outputTokens",))
