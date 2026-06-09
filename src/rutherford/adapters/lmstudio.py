# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The LM Studio adapter (``lmstudio``): a local model served on the same machine.

Like the Ollama adapter, this targets a model running locally rather than a cloud CLI, and stays
within Rutherford's thesis by driving LM Studio's own command-line program -- ``lms chat <model>
-p <prompt>`` ("print response to stdout and quit") -- rather than calling LM Studio's
OpenAI-compatible HTTP server directly. ``lms chat`` JIT-loads the model on demand, so no separate
``lms load`` and no running server are required.

Bring-your-own-model: the model is the LM Studio model key the delegation names (``target.model``),
e.g. ``google/gemma-4-12b``. There is no built-in default -- when a call names no model the
configured ``[adapters.lmstudio] default_model`` is used (the delegation service fills it in), and
if neither is set the adapter raises a clear error. ``available_models`` lists the installed LLMs by
parsing ``lms ls --json``.

Remote models work transparently: a model loaded on another machine over LM Studio's **LM Link**
appears in ``lms ls`` with its normal model key, and ``lms chat`` routes to whichever device has it
(preferring an already-loaded instance), so a remote model needs no special handling here -- pass the
plain model key, not a device-qualified one (``lms chat`` rejects ``<deviceId>:<modelKey>``).

LM Studio's CLI has a native ``-s/--system-prompt`` flag, so the role preamble rides there rather
than being prepended to the prompt. Auth is a non-issue for local inference (``lms login`` is only
for publishing to LM Studio Hub). Sampling params live in the model's LM Studio config, not the CLI.
``--ttl`` (how long to keep the model resident after the call) and any other ``lms chat`` flags can
be set through ``[adapters.lmstudio] extra_args``.

Output quirk: ``lms chat`` streams the model-load progress bar to **stdout** (carriage-return
overwrites, not stderr), and a reasoning model emits a ``<think>...</think>`` block before its
answer. ``parse_output`` renders the carriage-return progress away and strips the think block so the
normalized text is just the answer.
"""

from __future__ import annotations

import json
import re

from ..domain.enums import AuthState, OutputMode, SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
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
from .results import error_result, nonzero_result, strip_ansi, success_result, timeout_result

#: A reasoning model's chain-of-thought leading block, which ``lms chat`` prints before the answer.
#: Anchored to ``\A`` so only a single *leading* block is removed; a literal ``<think>`` tag that
#: appears in the middle of the model's answer (e.g. in an explanation of reasoning models or an
#: XML/regex example) is left intact.
_THINK_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.DOTALL)


class LMStudioAdapter(BaseCLIAdapter):
    """Adapter for a local LM Studio model, driven via ``lms chat <model> -p <prompt>``."""

    id = "lmstudio"
    display_name = "LM Studio (local model)"
    binary = "lms"
    version_args = ("version",)
    #: A local model is opt-in: not everyone runs one, so its absence is never an error.
    optional = True

    def check_auth(self) -> AuthStatus:
        """Local inference needs no credentials; report authenticated.

        ``lms login`` authenticates with LM Studio Hub for publishing artifacts, not for running a
        local model, so a logged-out LM Studio still serves models.
        """
        return AuthStatus(state=AuthState.AUTHENTICATED, detail="local LM Studio requires no credentials")

    def available_models(self) -> list[str]:
        """List installed LLM model keys by parsing ``lms ls --json``; empty on any failure."""
        result = self._probe.run([self.binary, "ls", "--json"], timeout_s=15.0)
        if result.exit_code != 0:
            return []
        return _parse_model_keys(result.stdout)

    def _detect_version(self) -> str | None:
        """Report the ``lms`` build, taken from the ``CLI commit: <hash>`` line.

        ``lms version`` prints an ANSI banner first, so the base "first non-empty line" rule would
        report the banner art. Pull the commit hash instead.
        """
        result = self._probe.run([self.binary, *self.version_args], timeout_s=15.0)
        if result.exit_code != 0:
            return None
        text = strip_ansi(result.stdout or result.stderr)
        match = re.search(r"CLI commit:\s*(\S+)", text)
        return f"commit {match.group(1)}" if match else None

    def capabilities(self) -> AdapterCapabilities:
        """Advertise LM Studio's flags: model selection, list-models, native system prompt, text."""
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=True,
            supports_working_dir=False,
            supports_file_context=False,
            supports_list_models=True,
            supports_system_prompt=True,
            output_mode=OutputMode.TEXT,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Every mode maps to no flags: ``lms chat`` only generates text and cannot mutate."""
        return SafetyFlags(args=[], note="lms chat only generates text; it cannot modify the workspace")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build ``lms chat <model> -p <prompt>`` (with ``-s <preamble>`` when a role is set).

        Pure: returns an argv list, never a shell command line and never a subprocess. The prompt is
        its own argv element after ``-p`` (``lms`` is a real binary, so no shell-quoting concern; the
        only caveat is the OS command-line length limit on a very large prompt). LM Studio has a
        native system-prompt flag, so the role preamble rides in ``-s`` rather than being prepended;
        only the in-scope file list is folded into the prompt. Any ``[adapters.lmstudio] extra_args``
        the service resolved (e.g. ``--ttl 3600``) are appended. With no model resolvable, raise
        ``INVALID_INPUT`` rather than guess a model that may not exist.
        """
        model = req.target.model
        if not model:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                "no LM Studio model specified -- pass model= or set [adapters.lmstudio] default_model "
                "in your Rutherford config (run `lms ls` to see your installed models).",
            )
        prompt = self._with_files(req.prompt, req.files)
        argv = [self.binary, "chat", model, "-p", prompt]
        if ctx.role_preamble:
            argv += ["-s", ctx.role_preamble]
        argv += ctx.extra_args
        return InvocationSpec(argv=argv, cwd=req.working_dir)

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Return the model's text answer, cleaned of the load-progress bar and any think block."""
        if raw.timed_out:
            return timeout_result(ctx, raw)
        if raw.exit_code not in (0, None):
            return nonzero_result(ctx, raw)
        text = _clean_output(raw.stdout)
        if "<think>" in text and "</think>" not in text:
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                "lms output contains an unterminated <think> block "
                "(the model output was likely truncated mid-reasoning)",
            )
        if not text:
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                "lms produced no output (the model may have failed to load)",
            )
        return success_result(ctx, raw, text)


def _clean_output(stdout: str) -> str:
    """Strip the load-progress bar, ANSI, and any ``<think>`` block, leaving the answer.

    ``lms chat`` writes its model-load progress to stdout as carriage-return overwrites on one line.
    After removing ANSI, normalize CRLF, then collapse each line to what survives the last bare
    carriage return -- exactly what a terminal would render -- which drops the progress run. Finally
    remove a ``<think>...</think>`` reasoning block.
    """
    text = strip_ansi(stdout).replace("\r\n", "\n")
    rendered = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
    return _THINK_RE.sub("", rendered).strip()


def _parse_model_keys(stdout: str) -> list[str]:
    """Return the unique LLM model keys from ``lms ls --json`` (skipping embedding models)."""
    try:
        entries = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    keys: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "llm":
            continue
        key = entry.get("modelKey")
        if isinstance(key, str) and key and key not in keys:
            keys.append(key)
    return keys
