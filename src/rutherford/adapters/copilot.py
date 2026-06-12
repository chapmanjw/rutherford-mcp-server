# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The GitHub Copilot CLI adapter (``copilot``).

Invocation: ``copilot -p "<prompt>" --output-format json --no-auto-update --no-color`` with the
composed prompt as the ``-p`` argv value (Copilot has no stdin prompt sentinel and no headless
system-prompt flag, so the role preamble and in-scope file list are folded into the prompt).
``--model`` selects a model, ``-C`` sets the working directory (also passed as ``--add-dir`` so the
agent may read it), and ``--resume=<id>`` resumes a prior session. ``--no-auto-update`` pins the CLI
(Copilot self-updates and has shipped silent breaking changes), and ``--no-color`` keeps the JSONL
clean.

``--output-format json`` emits JSONL, one event per line. The final answer is the ``data.content`` of
the last non-empty ``assistant.message`` event (the streamed ``assistant.message_delta`` /
``assistant.reasoning`` events are marked ``ephemeral`` and ignored), the session id is the terminal
``result`` event's ``sessionId``, and that event's ``exitCode`` is the in-band success verdict. Token
usage is not in the stream (it lives in OTEL side-channel files), so ``cost`` stays ``None``; the
``result`` event carries only a ``premiumRequests`` quota figure, which is neither USD nor tokens.

Auth is a GitHub token (``COPILOT_GITHUB_TOKEN`` -> ``GH_TOKEN`` -> ``GITHUB_TOKEN``, fine-grained PAT
with the Copilot Requests permission) or a persisted ``copilot`` ``/login`` session under
``~/.copilot``. A classic ``ghp_`` token is *silently ignored* by Copilot, so it is reported as a
login problem with an actionable message rather than a false ``AUTHENTICATED``.

Flags and the JSONL event shape verified 2026-06-11 against ``copilot --help`` /
``copilot help permissions`` and a live ``copilot -p ... --output-format json`` capture
(GitHub Copilot CLI 1.0.61).
"""

from __future__ import annotations

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
from .parsing import finalize_answer, parse_jsonl
from .results import timeout_result


class CopilotAdapter(BaseCLIAdapter):
    """Adapter for the GitHub Copilot CLI (``copilot``)."""

    id = "copilot"
    display_name = "GitHub Copilot CLI"
    binary = "copilot"
    #: Only the documented ``auto`` sentinel ("let Copilot pick") is advertised. GitHub rotates the
    #: concrete model ids and there is no non-interactive list-models command, so naming a specific id
    #: here would break the moment it is renamed (a live run rejects ``--model <stale-id>``). A caller
    #: can still pin any currently-valid id explicitly via the delegate ``model`` argument.
    static_models = ("auto",)
    #: Copilot fronts several vendors (Anthropic / OpenAI / Google) behind one GitHub subscription, so
    #: the provider depends on the chosen model; :meth:`BaseCLIAdapter.provenance` infers it from the
    #: model id rather than asserting a fixed vendor.
    provider = None

    def __init__(self, probe: CommandProbe | None = None, *, data_root: Path | None = None) -> None:
        super().__init__(probe)
        #: Where a persisted ``copilot`` ``/login`` session lives. Injectable so the auth probe is
        #: unit-testable against a temp dir, mirroring the Antigravity adapter.
        self._data_root = data_root if data_root is not None else Path.home() / ".copilot"

    def check_auth(self) -> AuthStatus:
        """Resolve auth from a GitHub token, then a persisted session, never triggering a login.

        Precedence mirrors Copilot's own: ``COPILOT_GITHUB_TOKEN`` -> ``GH_TOKEN`` -> ``GITHUB_TOKEN``.
        A classic ``ghp_`` token is accepted by the env var but silently ignored by Copilot (it needs a
        fine-grained PAT with the Copilot Requests permission), so that value is reported as a login
        problem with an actionable message rather than a false positive. With no usable token, a
        ``~/.copilot/config.json`` left by a prior ``/login`` counts as a persisted session; the keychain
        path cannot be verified cheaply, so a keychain-only install reads as ``NEEDS_LOGIN`` and
        ``doctor``'s live check is the backstop.
        """
        present = self._env_present("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
        if present is not None:
            value = self._env_value("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
            if value is not None and value.startswith("ghp_"):
                return AuthStatus(
                    state=AuthState.NEEDS_LOGIN,
                    detail="a classic ghp_ token is silently ignored by Copilot; use a fine-grained PAT "
                    "with the Copilot Requests permission, or run `copilot` then /login",
                )
            return AuthStatus(state=AuthState.AUTHENTICATED, detail=f"{present} is set")
        if (self._data_root / "config.json").exists():
            return AuthStatus(state=AuthState.AUTHENTICATED, detail="persisted copilot session detected")
        return AuthStatus(
            state=AuthState.NEEDS_LOGIN,
            detail="set COPILOT_GITHUB_TOKEN/GH_TOKEN/GITHUB_TOKEN (fine-grained PAT, Copilot Requests "
            "scope) or run `copilot` then /login",
        )

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Copilot's feature flags (JSONL output, resume, model/dir selection)."""
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
        """Map every SafetyMode to Copilot tool-permission flags, defaulting to read-only.

        Copilot's tool kinds are ``write`` (every file create/modify tool) and ``shell(command)``;
        denial always beats ``--allow-all-tools``. In headless ``-p`` mode a tool needing approval has
        no one to answer it, so read modes pass ``--allow-all-tools`` (read/search tools run without a
        prompt) while denying ``write`` and ``shell`` (no mutation, no command execution). ``write``
        mode lifts the ``write`` denial but still denies ``shell`` (edits, not arbitrary commands).
        ``yolo`` is the documented bypass (``--yolo`` == all tools, paths, and URLs) and is never the
        default. ``--no-ask-user`` keeps the agent from blocking on the ask-user tool in every mode.
        """
        if mode is SafetyMode.YOLO:
            return SafetyFlags(args=["--yolo", "--no-ask-user"], note="bypass: all tools, paths, and URLs granted")
        if mode is SafetyMode.WRITE:
            return SafetyFlags(
                args=["--allow-all-tools", "--deny-tool", "shell", "--no-ask-user"],
                note="write: file edits auto-approved; shell denied",
            )
        return SafetyFlags(
            args=["--allow-all-tools", "--deny-tool", "write", "--deny-tool", "shell", "--no-ask-user"],
            note="read-only: reads auto-run, writes and shell denied",
        )

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build the ``copilot -p`` invocation. Pure; argv list only, never a shell string.

        Copilot has no headless system-prompt flag and no ``-p``-mode file-attach flag, so the role
        preamble and the in-scope file list are folded into the prompt, which rides as the single
        ``-p`` argv value. ``-C`` sets the working directory and ``--add-dir`` lets the agent read it.
        """
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "-p", prompt, "--output-format", "json", "--no-auto-update", "--no-color"]

        if req.target.model:
            argv += ["--model", req.target.model]
        if req.working_dir:
            argv += ["-C", req.working_dir, "--add-dir", req.working_dir]
        if req.session_id:
            argv += [f"--resume={req.session_id}"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args

        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Parse the JSONL event stream into the normalized envelope, defensively.

        Walks events line by line: the last non-empty ``assistant.message`` ``data.content`` is the
        answer, the terminal ``result`` event gives the ``sessionId`` and an ``exitCode`` verdict, and
        an ``error`` event (or a non-zero process exit) is a failure. Streamed ``ephemeral`` deltas are
        ignored. Never raises.
        """
        if raw.timed_out:
            return timeout_result(ctx, raw)

        events = parse_jsonl(raw.stdout)

        session_id: str | None = None
        answer: str | None = None
        failure: str | None = None

        for event in events:
            etype = event.get("type")
            if etype == "assistant.message":
                content = _message_content(event.get("data"))
                if content:
                    answer = content
            elif etype == "result":
                sid = event.get("sessionId")
                if sid:
                    session_id = str(sid)
                exit_code = event.get("exitCode")
                # Only the generic exit-code fallback -- a specific `error` event message (which arrives
                # earlier in the stream) is more informative, so it is never overwritten here.
                if isinstance(exit_code, int) and exit_code != 0 and failure is None:
                    failure = f"copilot reported exit code {exit_code}"
            elif etype == "error":
                failure = _error_message(event)

        return finalize_answer(
            ctx,
            raw,
            answer=answer,
            session_id=session_id,
            failure=failure,
            no_output_message="copilot --output-format json produced no assistant message",
        )

    def check_output_contract(self, raw: ProcessResult) -> bool:
        """A successful copilot run must emit at least one JSONL event (``--output-format json``)."""
        return bool(parse_jsonl(raw.stdout))


def _message_content(data: Any) -> str | None:
    """Return an ``assistant.message`` event's answer text, or ``None``.

    ``data.content`` is a plain string in the common case; a multi-part content list (text parts) is
    joined defensively. Empty content (a tool-call-only turn) returns ``None`` so a later non-empty
    message wins as the answer.
    """
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        return "".join(parts) or None
    return None


def _error_message(event: dict[str, Any]) -> str:
    """Pull a human message out of an ``error`` event, falling back to a generic string."""
    data = event.get("data")
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    if event.get("message"):
        return str(event["message"])
    return "copilot reported an error"
