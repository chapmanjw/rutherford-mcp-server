# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Mistral Vibe adapter (``vibe``).

Invocation: ``vibe --output json --trust --agent <mode> -p "<prompt>"`` with the composed prompt as
the ``-p`` argv value (Vibe is a Python console script, so a multi-line argv element is safe -- no npm
``.cmd`` shim). Vibe has no headless system-prompt flag and no ``-p``-mode file-attach flag, so the role
preamble and the in-scope file list are folded into the prompt. ``--workdir`` sets the working
directory (also where ``--trust`` grants non-interactive file access), and the safety ``--agent``
escalates write capability.

Two Windows-specific quirks are handled in :meth:`build_invocation`, both found by live verification and
neither obvious from ``--help``:

* ``vibe -p`` blocks until it sees **stdin EOF** before it runs. The adapter sets no stdin, so the
  runner attaches ``DEVNULL`` (an immediate EOF) and Vibe proceeds; a console that leaves stdin open
  hangs forever.
* Vibe writes its JSON to stdout using Python's locale codec, which is cp1252 on Windows. Any answer
  carrying a non-cp1252 character (an arrow, em-dash, smart quote, emoji, accented letter) makes Vibe
  crash mid-write with a ``charmap`` ``UnicodeEncodeError`` and a truncated array. Setting
  ``PYTHONIOENCODING=utf-8`` / ``PYTHONUTF8=1`` in the child env forces UTF-8 stdout and removes the
  crash. (A truncated array from any other cause still fails cleanly as a non-zero exit / parse error,
  never a silent wrong answer.)

There is no ``--model`` flag: the model is the ``active_model`` config field, overridable per call via
the ``VIBE_ACTIVE_MODEL`` env var, which is how the adapter honors ``target.model``. ``--output json``
prints a top-level JSON array of message objects (``role`` / ``content`` plus null metadata); the answer
is the last ``role == "assistant"`` element's ``content``. The array carries no session id and no cost
figure, so ``supports_resume`` is False and ``cost`` stays ``None``.

Auth is ``MISTRAL_API_KEY`` or a persisted ``~/.vibe/.env`` left by ``vibe --setup``. Flags and the
JSON shape verified 2026-06-11 against ``vibe --help`` and a live ``vibe -p ... --output json`` capture
(vibe 2.14.1, model devstral-small).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from .parsing import finalize_answer, parse_json_array
from .results import error_result, nonzero_result, timeout_result


class VibeAdapter(BaseCLIAdapter):
    """Adapter for Mistral's Vibe CLI (``vibe``)."""

    id = "vibe"
    display_name = "Mistral Vibe"
    binary = "vibe"
    static_models: tuple[str, ...] = ()
    version_args = ("--version",)
    #: Vibe fronts first-party Mistral / Devstral by default, but its ``local`` model alias routes to a
    #: local llama.cpp endpoint, so the vendor is a best-guess home default, not confirmed.
    provider = "mistral"

    def __init__(self, probe: CommandProbe | None = None, *, data_root: Path | None = None) -> None:
        super().__init__(probe)
        #: Where ``vibe --setup`` persists credentials (``~/.vibe/.env``). Injectable so the auth probe
        #: is unit-testable against a temp dir, mirroring the Antigravity adapter.
        self._data_root = data_root if data_root is not None else Path.home() / ".vibe"

    def check_auth(self) -> AuthStatus:
        """Resolve auth from ``MISTRAL_API_KEY``, then a persisted ``~/.vibe/.env``, never logging in.

        Vibe loads its own ``~/.vibe/.env`` dotenv at startup, so the key need not be in the process
        environment. A present ``.env`` is therefore the on-disk "has been set up" marker; its mere
        existence does not prove the key inside is valid, so ``doctor``'s live check is the backstop.
        """
        present = self._env_present("MISTRAL_API_KEY")
        if present is not None:
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        if (self._data_root / ".env").exists():
            return AuthStatus(
                state=AuthState.AUTHENTICATED,
                detail="persisted Vibe credentials detected (~/.vibe/.env)",
            )
        return AuthStatus(
            state=AuthState.API_KEY_MISSING,
            detail="set MISTRAL_API_KEY or run `vibe --setup`",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Vibe's feature flags (JSON-array output, model selection via env, working dir)."""
        return AdapterCapabilities(
            supports_resume=False,  # --output json carries no session id to round-trip
            supports_model_selection=True,  # via the VIBE_ACTIVE_MODEL env override (no --model flag)
            supports_working_dir=True,  # --workdir
            supports_file_context=True,  # folded into the prompt (no -p file-attach flag)
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.JSON,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Map every SafetyMode to a Vibe ``--agent``, defaulting to the no-edit ``plan`` agent.

        Vibe's built-in agents are ``default`` (config-driven approvals, would prompt and hang headless),
        ``plan`` (no edits), ``accept-edits`` (auto-approve file edits), and ``auto-approve`` (run all
        tools without approval). read_only and propose use ``plan``; write uses ``accept-edits``; yolo
        uses ``auto-approve`` (the bypass, never the default).
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--agent", "auto-approve"], note="auto-approve: tools run without approval")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(args=["--agent", "accept-edits"], note="accept-edits: file edits auto-approved")
        return SafetyFlags(args=["--agent", "plan"], note="plan: no edits applied")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``vibe -p`` invocation. Pure; argv list only, never a shell string.

        Sets no stdin so the runner attaches ``DEVNULL`` (the stdin EOF ``vibe -p`` waits for), and forces
        UTF-8 stdout via the child env so a non-cp1252 answer character cannot crash Vibe on Windows.
        ``-p`` is placed last so the prompt is the final token, and the model rides on ``VIBE_ACTIVE_MODEL``
        because Vibe has no ``--model`` flag.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "--output", "json", "--trust"]
        if req.working_dir:
            argv += ["--workdir", req.working_dir]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        argv += ["-p", prompt]

        env = {**safety.env, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        if req.target.model:
            env["VIBE_ACTIVE_MODEL"] = req.target.model

        return InvocationSpec(argv=argv, env=env, cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSON message array into the normalized envelope, defensively.

        ``--output json`` prints a JSON array of message objects; the answer is the last ``assistant``
        message's ``content``. Output that is not a JSON array (an error, or a crash-truncated array)
        is a non-zero exit or a parse error; a clean array with no assistant text is a parse error.
        Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        messages = parse_json_array(raw.stdout)
        if messages is None:
            if raw.exit_code != 0:
                return nonzero_result(ctx, raw)
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                "vibe --output json produced no parseable JSON array",
                text=raw.stdout.strip(),
            )

        return finalize_answer(
            ctx,
            raw,
            answer=_last_assistant_content(messages),
            no_output_message="vibe --output json produced no assistant message",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful vibe run must emit a parseable JSON message array (``--output json``)."""
        return bool(parse_json_array(raw.stdout))


def _last_assistant_content(messages: list[dict[str, Any]]) -> str | None:
    """Return the last ``assistant`` message's text content, or ``None``.

    Skips assistant turns whose ``content`` is empty (a tool-call-only turn), so the final spoken
    answer wins. ``content`` is a plain string in the common case; a multi-part list is joined.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        text = _content_text(message.get("content"))
        if text:
            return text
    return None


def _content_text(content: Any) -> str | None:
    """Coerce a message ``content`` (string or list of text parts) to text, or ``None``."""
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [str(part.get("text")) for part in content if isinstance(part, dict) and part.get("text")]
        return "".join(parts) or None
    return None
