# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The Ollama adapter (``ollama``): a local model served on the same machine.

Unlike the cloud coding CLIs, this targets a model running under a local `Ollama
<https://ollama.com>`_ daemon. It stays within Rutherford's thesis by driving Ollama's own
command-line program as a subprocess -- ``ollama run <model>`` with the prompt fed on **stdin** --
rather than calling the Ollama HTTP API directly.

Bring-your-own-model: the model is whatever the delegation names (``target.model``), so one adapter
fronts every pulled model. There is no built-in default -- when a call names no model the configured
``[adapters.ollama] default_model`` is used (the delegation service fills it in), and if neither is
set the adapter raises a clear error rather than guessing a model that may not be installed.
``available_models`` lists what is installed by parsing ``ollama list``.

Ollama only generates text -- it has no tools and cannot touch the workspace -- so every
``SafetyMode`` maps to no flags, there is no system-prompt flag (the role preamble is prepended to
the prompt), and no session resume. Auth is a non-issue for a local daemon. A reasoning model
streams its chain-of-thought to stdout by default, so the adapter passes ``--hidethinking`` to keep
that trace out of the answer (the model still reasons internally); on a non-reasoning model it is a
no-op. Per-call sampling params (``num_ctx``/``temperature``/``num_predict``) come from the model's
Modelfile -- the CLI exposes no flags for them -- but flags that *do* exist (residency via
``--keepalive``, ``--format json``) can be set through ``[adapters.ollama] extra_args``, which the
service resolves and the adapter appends.

``--hidethinking`` requires a reasonably current Ollama (the flag shipped with the 2025 "thinking"
release); on an older build it would be rejected, so pin a recent Ollama.
"""

from __future__ import annotations

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


class OllamaAdapter(BaseCLIAdapter):
    """Adapter for a local Ollama model, driven via ``ollama run <model>``."""

    id = "ollama"
    display_name = "Ollama (local model)"
    binary = "ollama"
    version_args = ("--version",)
    #: A local model is opt-in: not everyone runs one, so its absence is never an error.
    optional = True

    def check_auth(self) -> AuthStatus:
        """A local daemon needs no credentials; report authenticated."""
        return AuthStatus(state=AuthState.AUTHENTICATED, detail="local Ollama daemon requires no credentials")

    def available_models(self) -> list[str]:
        """List installed models by parsing ``ollama list``; empty on any failure."""
        result = self._probe.run([self.binary, "list"], timeout_s=15.0)
        if result.exit_code != 0:
            return []
        return _parse_model_names(result.stdout)

    def _detect_version(self) -> str | None:
        """Read the Ollama version, ignoring the daemon-down warning preamble.

        ``ollama --version`` exits 0 even when the daemon is not running, but then writes a
        ``Warning: could not connect to a running Ollama instance`` line (and a ``Warning: client
        version is <X>`` line) to stdout. Take the version from whichever line carries the
        ``version is <X>`` token rather than blindly the first line, so ``capabilities``/``doctor``
        never display the warning string as the version.
        """
        result = self._probe.run([self.binary, *self.version_args], timeout_s=15.0)
        if result.exit_code != 0:
            return None
        text = (result.stdout or result.stderr).strip()
        for line in text.splitlines():
            match = re.search(r"version is (\S+)", line)
            if match:
                return match.group(1)
        return None

    def capabilities(self) -> AdapterCapabilities:
        """Advertise Ollama's flags: model selection and list-models, plain text, nothing else."""
        return AdapterCapabilities(
            supports_resume=False,
            supports_model_selection=True,
            supports_working_dir=False,
            supports_file_context=False,
            supports_list_models=True,
            supports_system_prompt=False,
            output_mode=OutputMode.TEXT,
        )

    def map_safety(self, mode: SafetyMode) -> SafetyFlags:
        """Every mode maps to no flags: ``ollama run`` only generates text and cannot mutate."""
        return SafetyFlags(args=[], note="ollama run only generates text; it cannot modify the workspace")

    def build_invocation(self, req: DelegationRequest, ctx: InvocationContext) -> InvocationSpec:
        """Build ``ollama run <model> --hidethinking --nowordwrap`` with the prompt fed on stdin.

        Pure: returns an argv list and an stdin string, never a shell command line and never a
        subprocess. Ollama has no system-prompt flag, so the role preamble is prepended to the
        prompt. ``--hidethinking`` keeps a reasoning model's chain-of-thought out of stdout so the
        answer is clean (the model still reasons internally); on a non-reasoning model it is a
        no-op. ``--nowordwrap`` disables the CLI's interactive word-wrap renderer, which runs even
        when stdout is a pipe: at each ~80-col wrap it prints the start of the word, "deletes" it
        with cursor escapes (``ESC[ND ESC[K``, N = the fragment's length), and reprints the word on
        the next line — stripping the ANSI then leaves the fragment behind, duplicating words at
        every wrap boundary (verified against ollama 2026-06; without the flag every long
        code/comment line corrupts).
        Any ``[adapters.ollama] extra_args`` the service resolved (e.g. ``--keepalive 30s``) are
        appended. The model is the target's model (the service fills it from
        ``[adapters.ollama] default_model`` when the call omits one); with no model resolvable,
        raise ``INVALID_INPUT`` rather than guess a model that may not exist.
        """
        model = req.target.model
        if not model:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                "no Ollama model specified -- pass model= or set [adapters.ollama] default_model "
                "in your Rutherford config (run `ollama list` to see your installed models).",
            )
        prompt = self._compose_prompt(req.prompt, ctx.role_preamble)
        return InvocationSpec(
            argv=[self.binary, "run", model, "--hidethinking", "--nowordwrap", *ctx.extra_args],
            cwd=req.working_dir,
            stdin=prompt,
        )

    def parse_output(self, raw: ProcessResult, ctx: InvocationContext) -> DelegationResult:
        """Return the model's text answer, or a normalized failure on timeout/non-zero/empty output."""
        if raw.timed_out:
            return timeout_result(ctx, raw)
        if raw.exit_code not in (0, None):
            return nonzero_result(ctx, raw)
        text = strip_ansi(raw.stdout).strip()
        if not text:
            return error_result(
                ctx,
                raw,
                ErrorCode.PARSE_ERROR,
                "ollama produced no output (the model may have failed to load or the context overflowed)",
            )
        return success_result(ctx, raw, text)


def _parse_model_names(stdout: str) -> list[str]:
    """Return the model names from ``ollama list`` output (the first column, skipping the header)."""
    names: list[str] = []
    for line in stdout.splitlines()[1:]:  # the first line is the NAME/ID/SIZE/MODIFIED header
        tokens = line.split()
        if tokens:
            names.append(tokens[0])
    return names
