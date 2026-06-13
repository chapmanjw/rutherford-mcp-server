# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Kilo Code adapter (``kilo``).

Invocation: ``kilo run --format json "<prompt>"`` with the composed prompt as the trailing
**positional** message. ``kilo run`` is the non-interactive entry point; ``--format json`` emits a
JSONL event stream (logs go to stderr); ``-m <provider/model>`` selects a model, ``--dir`` sets the
working directory, and ``-s <id>`` resumes a session. The npm shim is launched through PowerShell (see
:func:`~rutherford.runtime.launch.prepare_argv`), so the multi-line positional prompt is not truncated
by ``cmd.exe``. Kilo spins a local server/DB per run, so it is comparatively slow -- a generous
``[adapters.kilo] timeout_s`` is recommended.

The stream is ``{"type":"text","part":{"type":"text","text":"<chunk>"}}`` chunks closed by a
``{"type":"step_finish","part":{"type":"step-finish","reason":"stop","tokens":{...},"cost":...}}``
event; ``sessionID`` rides on every event. The answer is the concatenation of the text chunks; cost is
read from the ``step-finish`` part.

SAFETY CAVEAT (read_only is best-effort; distinct write/yolo). ``kilo run`` auto-runs its tools in a
non-interactive run even without an approval flag -- verified live that a read_only delegation applied a
file edit -- so ``read_only`` / ``propose`` (which add no flag) are **best-effort, not a guaranteed
sandbox** (the Antigravity case; ``verify_read_only`` is the post-hoc backstop). ``write`` uses ``--auto``
(approve all permissions) and ``yolo`` uses ``--dangerously-skip-permissions`` (skip anything not
explicitly denied).
Kilo's ``--variant`` reasoning knob is provider-specific (``high``/``max``/``minimal`` are not a uniform
tier scale), so effort is a documented no-op here -- pass ``--variant`` via ``[adapters.kilo] extra_args``
when a specific provider supports it. Auth is a configured provider (``kilo auth list``), distinct from
the Kilo Gateway login; ``doctor`` is the live backstop.

Flags verified 2026-06-13 against ``kilo run --help`` (Kilo Code 7.3.45).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..domain.enums import OutputMode, SafetyMode
from ..domain.models import (
    AdapterCapabilities,
    AuthStatus,
    Cost,
    DelegationRequest,
    DelegationResult,
    InvocationContext,
    InvocationSpec,
    ProcessResult,
    SafetyFlags,
)
from .base import BaseCLIAdapter
from .parsing import finalize_answer, parse_jsonl
from .results import timeout_result


class KiloAdapter(BaseCLIAdapter):
    """Adapter for the Kilo Code CLI (``kilo``)."""

    id = "kilo"
    display_name = "Kilo Code"
    binary = "kilo"
    static_models: tuple[str, ...] = ()
    #: Kilo is bring-your-own-model (``kilo/<provider>/<model>`` ids across many vendors), so the vendor
    #: depends on the chosen model; provenance infers it from the resolved id.
    provider = None

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``kilo auth list`` (a configured provider), never logging in.

        Kilo separates provider credentials (``kilo auth``) from a Kilo Gateway login (``kilo profile``);
        a delegation needs the former, so ``kilo auth list``'s exit status is the signal, with ``doctor``
        as the live backstop.
        """
        return self._auth_from_env_or_command((), [self.binary, "auth", "list"])

    def available_models(self) -> list[str]:
        """List models via ``kilo models``, falling back to the static set.

        Output is one model id per line (``kilo/<provider>/<model>``); lines without a ``/`` (any stray
        banner) are skipped. Any failure returns the static set.
        """
        result = self._probe.run([self.binary, "models"], timeout_s=20.0)
        if result.exit_code != 0:
            return list(self.static_models)
        models = [line.strip() for line in result.stdout.splitlines() if "/" in line and line.strip()]
        return models or list(self.static_models)

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Kilo's feature flags (JSONL stream, resume, model/dir selection, list-models)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=True,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Kilo's approval posture, failing CLOSED.

        ``read_only`` / ``propose`` add no flag, but ``kilo run`` auto-runs its tools in a non-interactive
        run anyway (verified live that it applies an edit), so this is **best-effort** -- ``verify_read_only``
        is the post-hoc backstop. ``write`` uses ``--auto`` (approve all), ``yolo`` uses
        ``--dangerously-skip-permissions``. An unknown mode falls through to the no-flag posture -- never
        to a bypass flag.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(
                args=["--dangerously-skip-permissions"], note="auto-approve permissions not explicitly denied"
            )
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["--auto"], note="auto-approve all permissions")
        return SafetyFlags(
            args=[],
            note="best-effort: kilo run auto-runs tools non-interactively even with no flag; "
            "verify_read_only is the post-hoc backstop",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``kilo run --format json`` invocation. Pure; argv list only, never a shell string.

        The role preamble and in-scope files are folded into the prompt (Kilo has no system-prompt flag),
        and the composed prompt is the trailing positional message.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "run", "--format", "json"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        if req.target.model:
            argv += ["-m", req.target.model]
        if req.working_dir:
            argv += ["--dir", req.working_dir]
        if req.session_id:
            argv += ["-s", req.session_id]
        argv += list(ctx.extra_args)
        argv.append(prompt)

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL event stream into the normalized envelope, defensively.

        The answer is the concatenation of ``text`` chunks; ``sessionID`` is the session id; the
        ``step-finish`` part gives cost; an ``error`` event is a failure. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        chunks: list[str] = []
        session_id: str | None = None
        cost: Cost | None = None
        failure: str | None = None

        for event in parse_jsonl(raw.stdout):
            sid = event.get("sessionID")
            if sid and session_id is None:
                session_id = str(sid)
            etype = event.get("type")
            part = event.get("part")
            if etype == "text" and isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif etype == "step_finish" and isinstance(part, dict):
                cost = _kilo_cost(part) or cost
            elif etype == "error":
                failure = _kilo_error_message(event)

        answer = "".join(chunks).strip() if chunks else None
        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            cost=cost,
            failure=failure,
            no_output_message="kilo --format json produced no text output",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful kilo run must emit at least one JSONL event (``--format json``)."""
        return bool(parse_jsonl(raw.stdout))


def _kilo_error_message(event: dict[str, Any]) -> str:
    """Extract a usable message from a Kilo ``error`` event, across its nesting shapes.

    Kilo wraps a provider/runtime failure as ``{"type":"error","error":{"name":...,"data":{"message":...}}}``
    (e.g. an ``APIError`` carrying a ``PAID_MODEL_AUTH_REQUIRED`` body) -- so the message lives at
    ``error.data.message``, not on the event or its ``part``. Fall through name / part / top-level message
    so a future shape still yields something better than a bare default.
    """
    err = event.get("error")
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        if err.get("message"):
            return str(err["message"])
        if err.get("name"):
            return str(err["name"])
    part = event.get("part")
    if isinstance(part, dict) and part.get("message"):
        return str(part["message"])
    return str(event.get("message") or "kilo reported an error")


def _kilo_cost(part: dict[str, Any]) -> Cost | None:
    """Build a :class:`Cost` from a Kilo ``step-finish`` part (``tokens`` block + ``cost`` USD)."""
    tokens = part.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    usd = part.get("cost")
    input_tokens = tokens.get("input")
    output_tokens = tokens.get("output")
    total_tokens = tokens.get("total")
    if usd is None and input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    try:
        return Cost(usd=usd, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
    except ValidationError:
        return None
