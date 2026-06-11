# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Codex CLI adapter (OpenAI's ``codex``).

Fresh invocation: ``codex exec --json --skip-git-repo-check`` with the prompt read from **stdin**
(not argv). On Windows ``codex`` is an npm ``.cmd`` shim launched through ``cmd.exe /c``, which
truncates an argv element at the first newline -- so a multi-line prompt (role preamble + task +
files) must ride on stdin, never as a positional. ``-m`` selects a model, ``-C`` sets the working
root, and ``-s`` selects the sandbox policy. Codex has no system-prompt flag, so the role preamble
is folded into the prompt.

Resume invocation: ``codex exec resume [OPTIONS] -- <SESSION_ID> -`` with the prompt on stdin. The
``resume`` subcommand has a *different, smaller* option set than ``exec``: it rejects ``-s/--sandbox``
and ``-C/--cd`` (verified against ``codex exec resume --help``). So resume omits those, expresses the
sandbox via the accepted ``-c sandbox_mode=<mode>`` config override instead, relies on the process
cwd for the working directory, and passes ``SESSION_ID`` and the prompt as positionals after a ``--``
separator. The prompt positional is ``-`` (Codex's stdin sentinel), keeping the multi-line prompt on
stdin and away from the cmd.exe-shim newline hazard; ``--`` keeps a dash-bearing session id or prompt
from being parsed as a flag.

The ``--json`` flag emits JSONL events; the final answer is the text of the last
``item.completed`` agent message, the session id is the ``thread.started`` ``thread_id``, and
token usage comes from ``turn.completed``. A resume whose arguments the CLI rejects surfaces as a
distinct ``RESUME_FAILED`` (not an opaque ``NONZERO_EXIT``), so a lost resume is never silent. Auth
is read from ``codex doctor --json`` -- its ``auth.credentials`` check covers a custom/Bedrock model
provider too -- falling back to ``OPENAI_API_KEY`` / ``CODEX_API_KEY`` or a persisted
``~/.codex/auth.json`` session when that command is unavailable.

Flags verified 2026-05-30 against ``codex exec --help`` and ``codex exec resume --help``
(codex-cli 0.135.0).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..domain.enums import AuthState, OutputMode, SafetyMode
from ..domain.error_codes import ErrorCode
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
from ..runtime.platform import PlatformInfo, detect_platform
from ..runtime.probe import CommandProbe
from .base import BaseCLIAdapter
from .parsing import CostSpec, extract_cost, finalize_answer, last_json_object, parse_jsonl
from .results import error_result, timeout_result


class CodexAdapter(BaseCLIAdapter):
    """Adapter for OpenAI's Codex CLI (``codex``)."""

    id = "codex"
    display_name = "Codex"
    binary = "codex"
    static_models: tuple[str, ...] = ()
    version_args = ("--version",)
    #: OpenAI's CLI, but it can be pointed at an ``amazon-bedrock`` model provider, so this is the
    #: best-guess vendor, not a confirmed one (confirming would need a ``codex doctor`` subprocess).
    provider = "openai"

    def __init__(self, probe: CommandProbe | None = None, *, platform: PlatformInfo | None = None) -> None:
        super().__init__(probe)
        #: Resolved once at construction so ``map_safety`` stays a pure function of its inputs.
        #: Injectable so the Windows sandbox flag is unit-testable from any host.
        self._platform = platform if platform is not None else detect_platform()

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``codex doctor --json``, falling back to the env key / persisted session.

        ``codex doctor --json`` emits a health report whose ``checks["auth.credentials"].status``
        reflects the *effective* credential state -- including a custom or built-in ``amazon-bedrock``
        model provider, whose credential lives outside ``OPENAI_API_KEY`` / ``~/.codex/auth.json``
        (an AWS bearer token or the AWS SDK chain). That makes ``doctor`` the trustworthy signal: a
        Bedrock-configured Codex has neither cheap marker yet is fully authenticated, and
        ``codex doctor`` reports ``ok`` for it. Only when that command is unavailable (an older CLI)
        or carries no auth check do we fall back to the env key / persisted-session markers.
        """
        report = self._probe.run([self.binary, "doctor", "--json"], timeout_s=20.0)
        verdict = _doctor_auth_ok(report.stdout)
        if verdict is True:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="codex doctor reports auth.credentials ok")
        if verdict is False:
            return AuthStatus(
                state=AuthState.NEEDS_LOGIN,
                detail="codex doctor reports an auth problem; run codex login or set OPENAI_API_KEY",
            )
        present = self._env_present("OPENAI_API_KEY", "CODEX_API_KEY")
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        if (Path.home() / ".codex" / "auth.json").exists():
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="persisted session")
        return AuthStatus(
            state=AuthState.NEEDS_LOGIN,
            detail="run codex login or set OPENAI_API_KEY",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Codex's feature flags (JSONL output, resume, model/dir selection)."""
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=True,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSONL,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to Codex sandbox + approval flags, defaulting to read-only.

        Sandbox policy: read_only/propose use the read-only sandbox, write uses workspace-write, yolo
        uses the bypass flag. ``codex exec`` is non-interactive, so the sandboxed modes also pass
        ``-c approval_policy=never``: the default approval policy blocks any command Codex deems
        "untrusted" with no approver, which silently breaks even read commands (it cannot read files or
        run tools). The read-only sandbox still prevents writes; ``never`` only drops a prompt nothing
        could answer. On native Windows it adds ``-c windows.sandbox=unelevated`` so a headless, nested
        ``codex exec`` does not need the UAC-gated "elevated" sandbox setup, which fails with
        "windows sandbox: spawn setup refresh" when spawned as a deep child process; the flag is a no-op
        off Windows. yolo already bypasses both the sandbox and approvals, so it needs neither.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(
                args=["--dangerously-bypass-approvals-and-sandbox"],
                note="bypass all approvals and sandboxing",
            )
        sandbox = "workspace-write" if mode is SafetyMode.WRITE else "read-only"
        args = ["-s", sandbox, "-c", "approval_policy=never", *self._windows_sandbox_args()]
        return SafetyFlags(args=args, note=f"{sandbox} sandbox, non-interactive (approval_policy=never)")

    def _windows_sandbox_args(self) -> list[str]:
        """``-c windows.sandbox=unelevated`` on native Windows, else empty.

        Codex's default "elevated" Windows sandbox needs UAC/admin setup a headless, nested subprocess
        cannot obtain; "unelevated" is the documented fallback (a restricted token, no admin setup). See
        docs/troubleshooting.md.
        """
        return ["-c", "windows.sandbox=unelevated"] if self._platform.is_windows else []

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``codex exec`` (or ``codex exec resume``) invocation.

        Pure: returns an argv list and an stdin string, never a shell command line. The prompt
        carries the role preamble (Codex has no system-prompt flag) and any in-scope files, and
        always rides on stdin. ``resume`` takes a different option set than ``exec``, so it is
        built separately rather than threading a flag through the shared path.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        if req.session_id:
            return self._build_resume(req, ctx, prompt, req.session_id)
        return self._build_fresh(req, ctx, prompt)

    def _build_fresh(self, req: DelegationRequest, ctx: InvocationContext, prompt: str) -> InvocationSpec:
        """Build a fresh (non-resumed) ``codex exec`` invocation: prompt on stdin, no positional."""
        argv = [self.binary, "exec", "--json", "--skip-git-repo-check"]
        if req.working_dir:
            argv += ["-C", req.working_dir]
        if req.target.model:
            argv += ["-m", req.target.model]
        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir, stdin=prompt)

    def _build_resume(
        self, req: DelegationRequest, ctx: InvocationContext, prompt: str, session_id: str
    ) -> InvocationSpec:
        """Build a ``codex exec resume`` invocation.

        ``resume`` rejects ``-s/--sandbox`` and ``-C/--cd``; the session id and prompt are
        positionals. So: sandbox via ``-c sandbox_mode=`` (or the bypass flag for yolo), working
        directory via the process cwd (not ``-C``), and ``-- <session_id> -`` for the positionals
        with the prompt on stdin (the ``-`` sentinel). The ``--`` guards a dash-bearing id/prompt.
        """
        safety = self.map_safety(ctx.safety_mode)
        argv = [self.binary, "exec", "resume", "--json", "--skip-git-repo-check"]
        if req.target.model:
            argv += ["-m", req.target.model]
        argv += _resume_safety_args(safety.args)
        argv += ["--", session_id, "-"]
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir, stdin=prompt)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL event stream into the normalized envelope, defensively.

        Walks events line by line: ``thread.started`` gives the session id, the last
        ``item.completed`` agent message gives the answer, ``turn.completed`` gives token usage,
        and ``turn.failed`` / ``error`` (or a non-zero exit) yields a failure. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        events = parse_jsonl(raw.stdout)

        session_id: str | None = None
        answer: str | None = None
        cost: Cost | None = None
        failure: str | None = None

        for event in events:
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id")
                if thread_id:
                    session_id = str(thread_id)
            elif etype == "item.completed":
                text = _agent_message_text(event.get("item"))
                if text is not None:
                    answer = text
            elif etype == "turn.completed":
                cost = extract_cost(event.get("usage"), _COST) or cost
                # The terminal verdict wins: a turn that completed cleanly clears any transient
                # `error` event the CLI retried past, so a stale retry message cannot fail the run.
                failure = None
            elif etype == "turn.failed":
                err = event.get("error")
                if isinstance(err, dict):
                    failure = str(err.get("message") or "codex turn failed")
                else:
                    failure = "codex turn failed"
            elif etype == "error":
                failure = str(event.get("message") or "codex reported an error")

        # A rejected `codex exec resume` exits non-zero with a clap usage error and no answer on
        # stdout; surface it as a distinct RESUME_FAILED (not an opaque non-zero exit) so a lost
        # resume is never silent. Every other case is the shared answer/failure decision.
        if raw.exit_code != 0 and answer is None:
            parse_error = _argument_parse_error(raw.stderr)
            if parse_error is not None and _is_resume_error(raw.stderr):
                return error_result(
                    ctx,
                    raw,
                    ErrorCode.RESUME_FAILED,
                    f"`codex exec resume` rejected its arguments: {parse_error}. "
                    "This is a Rutherford/codex CLI mismatch, not a model error -- "
                    "retry without a session_id to start a fresh session.",
                    text="",
                )

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            cost=cost,
            failure=failure,
            no_output_message="codex --json produced no agent message",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful codex run must emit at least one JSONL event (``--json``).

        The drift canary for this adapter: if a future CLI build stops emitting the event stream
        but still exits cleanly, this is what catches the silent regression at the delegation layer.
        """
        return bool(parse_jsonl(raw.stdout))


def _doctor_auth_ok(stdout: str) -> bool | None:
    """Return the auth verdict from ``codex doctor --json``: ``True`` ok, ``False`` a problem, ``None`` absent.

    The report nests an ``auth.credentials`` check under ``checks``; its ``status`` is ``ok`` when the
    effective credential -- an OpenAI key, a persisted login, or a custom/``amazon-bedrock`` provider
    credential -- is usable. ``None`` means no parseable report or no such check, so the caller falls
    back to the cheap env-key / persisted-session markers.
    """
    data = _parse_doctor_json(stdout)
    if data is None:
        return None
    checks = data.get("checks")
    if not isinstance(checks, dict):
        return None
    auth_check = checks.get("auth.credentials")
    if not isinstance(auth_check, dict) or "status" not in auth_check:
        return None
    return str(auth_check.get("status")).strip().lower() == "ok"


def _parse_doctor_json(stdout: str) -> dict[str, Any] | None:
    """Return the JSON object from ``codex doctor --json``, tolerating pretty-print or surrounding noise.

    Try a whole-output parse first, then fall back to the robust embedded-object scanner: a
    find/rfind brace slice breaks on brace-bearing log noise around the report, which would
    silently downgrade a healthy (e.g. Bedrock) auth to NEEDS_LOGIN.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return whole
    return last_json_object(stdout)


def _resume_safety_args(exec_safety_args: list[str]) -> list[str]:
    """Translate the fresh-``exec`` safety flags into the form ``codex exec resume`` accepts.

    ``resume`` rejects ``-s/--sandbox`` but accepts ``-c <key=value>`` config overrides and the
    ``--dangerously-bypass-approvals-and-sandbox`` flag. So a ``["-s", "<mode>"]`` pair becomes
    ``["-c", "sandbox_mode=<mode>"]`` (the unquoted value parses as the literal mode string) while every
    other flag passes through unchanged -- the ``-c approval_policy=never`` /
    ``-c windows.sandbox=unelevated`` overrides and the yolo bypass flag. Deriving from ``map_safety``
    keeps the resume posture in lockstep with the fresh posture, no second source of truth to drift.
    """
    out: list[str] = []
    index = 0
    while index < len(exec_safety_args):
        arg = exec_safety_args[index]
        if arg in ("-s", "--sandbox") and index + 1 < len(exec_safety_args):
            out += ["-c", f"sandbox_mode={exec_safety_args[index + 1]}"]
            index += 2
        else:
            out.append(arg)
            index += 1
    return out


def _is_resume_error(stderr: str) -> bool:
    """Return whether a CLI error came from the ``resume`` subcommand (vs. a fresh ``exec``)."""
    return "exec resume" in stderr.lower()


def _argument_parse_error(stderr: str) -> str | None:
    """Return the first line of a clap argument-parse error, or ``None`` if stderr is not one.

    A malformed invocation (an option the subcommand does not accept, a bad value) makes Codex
    exit non-zero with a clap usage error and no JSONL on stdout -- categorically different from a
    model/runtime failure, and worth surfacing distinctly instead of as an opaque non-zero exit.
    """
    text = stderr.strip()
    if not text:
        return None
    lowered = text.lower()
    looks_like_parse_error = (
        "unexpected argument" in lowered
        or "invalid value" in lowered
        or "unrecognized subcommand" in lowered
        or "required arguments were not provided" in lowered
        or "cannot be used with" in lowered  # clap's conflicting-arguments wording
        # Any other clap usage error: an "error:" line followed by a "Usage:" block. Deliberately
        # not keyed on the "--help" hint -- a future CLI build could drop or reword that line, and
        # we must not silently demote a rejected resume to an opaque NONZERO_EXIT when it does.
        or (lowered.startswith("error:") and "usage:" in lowered)
    )
    if not looks_like_parse_error:
        return None
    return text.splitlines()[0].strip()


def _agent_message_text(item: Any) -> str | None:
    """Return the text of an agent-message item, handling both event shapes, or ``None``.

    The completed item is either ``{"type":"agent_message","text":...}`` or carries the message
    under ``{"details":{"type":"agent_message","text":...}}``. We accept whichever is present.
    """
    if not isinstance(item, dict):
        return None
    details = item.get("details")
    if isinstance(details, dict) and details.get("type") == "agent_message":
        text = details.get("text")
        return str(text) if text is not None else ""
    if item.get("type") == "agent_message":
        text = item.get("text")
        return str(text) if text is not None else ""
    return None


#: Codex carries token counts directly in a ``turn.completed`` ``usage`` block (``input_tokens`` /
#: ``output_tokens``), with no USD figure -- the default cost spec.
_COST = CostSpec()
