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

Auth is a Google account flow with no non-interactive ``whoami`` and no reliable, cross-platform
on-disk marker (the token location varies by OS and install -- native vs WSL, keyring vs file). A
cheap probe therefore cannot determine auth state, so ``check_auth`` returns ``unknown``; the
``doctor`` tool resolves that with a live round trip (the only trustworthy signal).

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
    Provenance,
    SafetyFlags,
)
from ..runtime.probe import CommandProbe
from .base import BaseCLIAdapter
from .results import error_result, nonzero_result, strip_ansi, success_result, timeout_result


class AntigravityAdapter(BaseCLIAdapter):
    """Adapter for Google's Antigravity CLI (``agy``)."""

    id = "antigravity"
    display_name = "Antigravity"
    binary = "agy"
    static_models = ()
    #: Google's CLI (the Gemini CLI successor); print mode serves this fixed Gemini model with no
    #: selector, so :meth:`provenance` can report a known model id instead of "unknown".
    _PRINT_MODEL = "gemini-3.5-flash"

    def __init__(self, probe: CommandProbe | None = None, *, data_root: Path | None = None) -> None:
        super().__init__(probe)
        self._data_root = data_root if data_root is not None else Path.home() / ".gemini" / "antigravity-cli"

    def check_auth(self) -> AuthStatus:
        # agy has no non-interactive whoami, and where it stores its token varies by platform and
        # install (keyring vs an on-disk file whose path differs native vs WSL), so no cheap probe
        # is trustworthy. Report unknown; doctor resolves it with a live round trip.
        return AuthStatus(
            state=AuthState.UNKNOWN,
            detail="agy has no non-interactive auth check; doctor verifies it with a live round trip",
        )

    def available_models(self) -> list[str]:
        # The print-mode model is fixed; do not pretend a selector exists.
        return []

    def provenance(self, ctx: InvocationContext) -> Provenance:
        """Google's CLI serving a fixed Gemini model. ``agy -p`` has no model selector, so the answer
        always comes from :attr:`_PRINT_MODEL` regardless of any (ignored) requested model -- report
        that, so the voice counts toward model diversity and ``confirmed`` is not a false claim."""
        return Provenance(provider="google", model=self._PRINT_MODEL, confirmed=True)

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
        # Check the exit code before reading the transcript: on a failed run the newest transcript
        # on disk may be stale (from a previous conversation), so it must not be reported as this
        # run's answer.
        if raw.exit_code not in (0, None):
            return nonzero_result(ctx, raw)

        conv_id, text = self._read_transcript(ctx.working_dir)
        if text:
            return success_result(ctx, raw, text, session_id=conv_id)
        debug = strip_ansi(raw.stdout).strip()
        return error_result(
            ctx,
            raw,
            ErrorCode.TRANSCRIPT_NOT_FOUND,
            "agy produced no readable transcript; pin the agy version and check the brain/ layout",
            text=debug,
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
        """Look the workspace up in the conversation index, else use the newest brain entry.

        When *working_dir* is provided but is absent from the index the run is likely a first
        delegation to that directory and the globally-newest brain entry belongs to a different
        conversation.  Return ``None`` in that case so ``parse_output`` emits
        ``TRANSCRIPT_NOT_FOUND`` rather than silently returning another conversation's answer.
        The global-newest fallback is only used when no *working_dir* was supplied at all.
        """
        index = self._data_root / "cache" / "last_conversations.json"
        if working_dir:
            if index.is_file():
                try:
                    mapping = json.loads(index.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    mapping = {}
                if isinstance(mapping, dict):
                    wanted = str(Path(working_dir))
                    for key, value in mapping.items():
                        if Path(key) == Path(wanted) and isinstance(value, str):
                            return value
            # working_dir was given but not in the index: do NOT fall back to the global newest.
            return None
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
