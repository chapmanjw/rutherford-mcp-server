# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Antigravity adapter (binary ``agy``) -- the transcript-quirk case.

Two quirks are handled entirely inside this adapter so nothing leaks upward:

* ``agy -p`` hardcodes the print-mode model (Gemini 3.5 Flash), so no model selector is exposed
  -- ``supports_model_selection`` is False and ``available_models`` is empty.
* ``agy -p`` stdout is unreliable, so ``parse_output`` reads the agent's transcript file instead.
  The transcript lives under ``~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/
  transcript.jsonl``; the conversation id is resolved from the workspace via
  ``cache/last_conversations.json``, falling back to the most recently modified ``brain`` entry.
  The final answer is the last line with ``source=MODEL``, ``status=DONE``,
  ``type=PLANNER_RESPONSE``, and non-empty content.

Auth is via the OS credential store (a Google account flow); there is no non-interactive way to
verify it, so ``check_auth`` reports ``unknown`` rather than hang.

Flags and transcript layout verified 2026-05-30 (agy 1.0.2). The transcript schema is community
reverse-engineered; pin the agy version and treat a schema change as a parse failure.
"""

from __future__ import annotations

import json
from pathlib import Path

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


class AntigravityAdapter(BaseCLIAdapter):
    """Adapter for Google's Antigravity CLI (``agy``)."""

    id = "antigravity"
    display_name = "Antigravity"
    binary = "agy"
    static_models = ()

    def __init__(self, probe: CommandProbe | None = None, *, data_root: Path | None = None) -> None:
        super().__init__(probe)
        self._data_root = data_root if data_root is not None else Path.home() / ".gemini" / "antigravity-cli"

    def check_auth(self) -> AuthStatus:
        # agy stores a Google OAuth token in the OS credential store and exposes no whoami; the
        # state cannot be probed without running the CLI interactively.
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="agy authenticates via the OS credential store; sign in once with `agy` interactively",
        )

    def available_models(self) -> list[str]:
        # The print-mode model is fixed; do not pretend a selector exists.
        return []

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_resume=True,
            supports_model_selection=False,
            supports_working_dir=True,
            supports_file_context=True,
            supports_list_models=False,
            supports_system_prompt=False,
            output_mode=OutputMode.TRANSCRIPT,
            file_context_style="add_dir",
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        # agy print mode has no granular approval. read_only/propose run without a bypass (so any
        # edit the agent attempts is simply not applied); write and yolo use the bypass flag,
        # which is the only way to let print mode apply changes.
        if mode in (SafetyMode.WRITE, SafetyMode.YOLO):
            return SafetyFlags(
                args=["--dangerously-skip-permissions"], note="bypass approvals (print mode has no granular approval)"
            )
        return SafetyFlags(args=[], note="default; edits are not applied in print mode without a bypass")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        prompt = self._with_files(self._compose_prompt(req.prompt, ctx.role_preamble), req.files)
        argv = [self.binary, "-p", prompt]

        if req.working_dir:
            argv += ["--add-dir", req.working_dir]
        if req.session_id:
            argv += ["--conversation", req.session_id]
        if req.timeout_s:
            argv += ["--print-timeout", f"{int(req.timeout_s)}s"]

        safety = self.map_safety(ctx.safety_mode)
        argv += safety.args
        return InvocationSpec(argv=argv, env=dict(safety.env), cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        if raw.timed_out:
            return timeout_result(ctx, raw)

        conv_id, text = self._read_transcript(ctx.working_dir)
        if text:
            return success_result(ctx, raw, text, session_id=conv_id)

        # No transcript text: fall back to stdout, then to a structured failure.
        if raw.exit_code not in (0, None):
            return nonzero_result(ctx, raw)
        if raw.stdout.strip():
            return success_result(ctx, raw, raw.stdout.strip(), session_id=conv_id)
        return error_result(
            ctx,
            raw,
            ErrorCode.TRANSCRIPT_NOT_FOUND,
            "agy produced no readable transcript; pin the agy version and check the brain/ layout",
        )

    # --- transcript handling -------------------------------------------------

    def _read_transcript(self, working_dir: str | None) -> tuple[str | None, str | None]:
        """Resolve the conversation id and extract the final assistant message.

        Returns ``(conversation_id, final_text)``; either element may be ``None`` when the
        transcript cannot be resolved or read.
        """
        conv_id = self._resolve_conversation_id(working_dir)
        if conv_id is None:
            return None, None
        transcript = self._data_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        text = self._extract_final_message(transcript)
        return conv_id, text

    def _resolve_conversation_id(self, working_dir: str | None) -> str | None:
        """Look the workspace up in the conversation index, else use the newest brain entry."""
        index = self._data_root / "cache" / "last_conversations.json"
        if working_dir and index.is_file():
            try:
                mapping = json.loads(index.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                mapping = {}
            if isinstance(mapping, dict):
                wanted = str(Path(working_dir))
                for key, value in mapping.items():
                    if Path(key) == Path(wanted) and isinstance(value, str):
                        return value
        return self._newest_brain_entry()

    def _newest_brain_entry(self) -> str | None:
        """Return the name of the most recently modified ``brain`` subdirectory, if any."""
        brain = self._data_root / "brain"
        if not brain.is_dir():
            return None
        entries = [child for child in brain.iterdir() if child.is_dir()]
        if not entries:
            return None
        newest = max(entries, key=lambda child: child.stat().st_mtime)
        return newest.name

    @staticmethod
    def _extract_final_message(transcript: Path) -> str | None:
        """Return the last completed planner response in the transcript, or ``None``."""
        if not transcript.is_file():
            return None
        final: str | None = None
        try:
            content = transcript.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("source") == "MODEL"
                and event.get("status") == "DONE"
                and event.get("type") == "PLANNER_RESPONSE"
                and isinstance(event.get("content"), str)
                and event["content"].strip()
            ):
                final = event["content"]
        return final
