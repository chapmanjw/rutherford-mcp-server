# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Antigravity adapter (binary ``agy``) -- the transcript-quirk case.

Two quirks are handled entirely inside this adapter so nothing leaks upward:

* ``agy -p`` hardcodes the print-mode model (Gemini 3.5 Flash), so no model selector is exposed
  -- ``supports_model_selection`` is False and ``available_models`` is empty.
* ``agy -p`` stdout is unreliable, so ``parse_output`` reads the agent's transcript file instead.
  The transcript lives under ``~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/
  transcript.jsonl``; the conversation id is the explicit resume id when one was passed, else resolved
  from the workspace via ``cache/last_conversations.json``, else the most recently modified ``brain``
  entry. The final answer is the last line with ``source=MODEL``, ``status=DONE``,
  ``type=PLANNER_RESPONSE``, and non-empty content.

Auth is a Google account flow with no non-interactive ``whoami`` and no reliable, cross-platform
on-disk marker (the token location varies by OS and install -- native vs WSL, keyring vs file). A
cheap probe therefore cannot determine auth state, so ``check_auth`` returns ``unknown``; the
``doctor`` tool resolves that with a live round trip (the only trustworthy signal).

Flags and transcript layout verified 2026-06-10 (agy 1.0.7). The transcript schema is community
reverse-engineered, so it is pinned (:attr:`AntigravityAdapter.verified_version`): ``doctor`` flags a
running agy whose version differs from the pin, and a transcript with a *completed* model turn that no
longer carries a ``PLANNER_RESPONSE`` answer is failed loudly as ``CONTRACT_MISMATCH`` (a drift signal,
distinct from a missing or in-progress transcript).

The headline, re-verified 2026-06-10 against agy 1.0.7: ``agy --print`` (``-p`` is its alias) still
emits nothing to stdout under a non-TTY pipe (the answer only ever lands in the transcript), so the
transcript read remains the only working capture path -- do not switch to reading ``--print`` stdout
until a stdout-recovery canary shows it carries the answer. Note: print mode is slow, so a generous
``[adapters.antigravity] timeout_s`` (e.g. 300) is recommended; the global default may truncate it.
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
    #: The agy version the flags and reverse-engineered transcript layout were last verified against.
    #: agy auto-updates, so ``doctor`` surfaces a note when the running version differs -- a prompt to
    #: re-verify the ``brain/`` layout and re-pin before the drift becomes a flood of failures.
    verified_version = "1.0.7"
    #: Google's CLI (the Gemini CLI successor); print mode serves this fixed Gemini model with no
    #: selector, so :meth:`provenance` can report a known model id instead of "unknown".
    _PRINT_MODEL = "gemini-3.5-flash"
    #: Seconds of headroom between agy's own ``--print-timeout`` (when it gives up and flushes) and the
    #: runner's hard tree-kill at the call's timeout, so a slow run exits cleanly instead of being
    #: killed mid-flush (which would leave a partial transcript).
    _PRINT_TIMEOUT_GRACE_S = 10

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
            # Give agy a margin to give up and flush its final transcript line before the runner's hard
            # tree-kill at req.timeout_s, so a slow run exits cleanly rather than killed mid-flush.
            agy_timeout = max(1, int(req.timeout_s) - self._PRINT_TIMEOUT_GRACE_S)
            argv += ["--print-timeout", f"{agy_timeout}s"]

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

        conv_id, text, completed_types = self._read_transcript(ctx.working_dir, ctx.session_id)
        if text:
            return success_result(ctx, raw, text, session_id=conv_id)
        debug = strip_ansi(raw.stdout).strip()
        if completed_types and "PLANNER_RESPONSE" not in completed_types:
            # A model turn COMPLETED (source=MODEL, status=DONE) but the expected PLANNER_RESPONSE answer
            # type is GONE -- the reverse-engineered schema has likely changed under a new agy (e.g. the
            # answer moved to a renamed type). Fail loudly as a drift signal (retryable + counts toward
            # cooldown via F7), naming the type(s) seen so the drift is self-diagnosing. A partial /
            # in-progress transcript (no completed turn) or a completed-but-empty PLANNER_RESPONSE is NOT
            # this case -- those fall through to TRANSCRIPT_NOT_FOUND.
            #
            # Deliberate assumption: PLANNER_RESPONSE is the sole terminal answer type. If a healthy
            # future agy moves its answer off PLANNER_RESPONSE entirely (a rename), this fires a
            # technically false CONTRACT_MISMATCH -- an acceptable trade, since a vanished answer type IS
            # a schema change worth a loud, retryable signal rather than trusting output we no longer
            # recognize. (An answer under a NEW type *alongside* a still-present PLANNER_RESPONSE just
            # succeeds.) When a new terminal type is confirmed, widen the accepted type in
            # _extract_final_message.
            return error_result(
                ctx,
                raw,
                ErrorCode.CONTRACT_MISMATCH,
                f"agy completed a model turn but the expected PLANNER_RESPONSE answer type is absent "
                f"(saw completed type(s): {', '.join(completed_types)}; verified against agy "
                f"{self.verified_version}) -- the transcript schema may have changed; re-verify and re-pin",
                text=debug,
            )
        return error_result(
            ctx,
            raw,
            ErrorCode.TRANSCRIPT_NOT_FOUND,
            "agy produced no readable transcript (no completed model turn); pin the agy version and "
            "check the brain/ layout",
            text=debug,
        )

    # --- transcript handling -------------------------------------------------

    def _read_transcript(
        self, working_dir: str | None, session_id: str | None
    ) -> tuple[str | None, str | None, list[str]]:
        """Resolve the conversation id and extract the final assistant message.

        Returns ``(conversation_id, final_text, completed_model_types)``. ``final_text`` is ``None``
        when no matching answer was found; ``completed_model_types`` is the distinct ``type`` of the
        completed model turns seen -- non-empty only when the model finished a turn, so the caller can
        tell a *schema drift* from an *absent / in-progress* transcript.
        """
        conv_id = self._resolve_conversation_id(working_dir, session_id)
        if conv_id is None:
            return None, None, []
        transcript = self._data_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        text, completed_types = self._extract_final_message(transcript)
        return conv_id, text, completed_types

    def _resolve_conversation_id(self, working_dir: str | None, session_id: str | None) -> str | None:
        """Resolve the conversation id whose transcript to read.

        An explicit *session_id* (a resumed conversation -- agy was passed ``--conversation <id>``) is
        authoritative and used directly. Otherwise look the workspace up in the conversation index;
        when *working_dir* is provided but absent from the index the run is likely a first delegation to
        that directory and the globally-newest brain entry belongs to a different conversation, so
        return ``None`` (``parse_output`` then emits ``TRANSCRIPT_NOT_FOUND`` rather than silently
        returning another conversation's answer). The global-newest fallback is used only when neither a
        session id nor a working_dir was supplied.
        """
        if session_id:
            return session_id
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
    def _extract_final_message(transcript: Path) -> tuple[str | None, list[str]]:
        """Return ``(last_planner_response, completed_model_types)`` from the transcript.

        ``last_planner_response`` is the content of the final ``source=MODEL`` / ``status=DONE`` /
        ``type=PLANNER_RESPONSE`` event, or ``None``. ``completed_model_types`` is the sorted distinct
        ``type`` values of the *completed* model turns seen (``source=MODEL`` and ``status=DONE``) --
        empty when the model never finished a turn.

        The caller uses ``completed_model_types`` to tell a genuine *schema drift* (a model turn
        completed, but under a type other than ``PLANNER_RESPONSE`` -- the answer shape changed) from a
        partial / in-progress / empty / absent transcript (no completed model turn, so simply no answer
        yet). A bare ``USER_INPUT`` line or a non-``DONE`` model event no longer counts as drift, and
        the surfaced types make a real drift self-diagnosing.
        """
        if not transcript.is_file():
            return None, []
        final: str | None = None
        completed_model_types: set[str] = set()
        try:
            content = transcript.read_text(encoding="utf-8")
        except OSError:
            return None, []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("source") == "MODEL" and event.get("status") == "DONE":
                event_type = event.get("type")
                if isinstance(event_type, str):
                    completed_model_types.add(event_type)
                if (
                    event_type == "PLANNER_RESPONSE"
                    and isinstance(event.get("content"), str)
                    and event["content"].strip()
                ):
                    final = event["content"]
        return final, sorted(completed_model_types)
