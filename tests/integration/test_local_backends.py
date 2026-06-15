# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration tests: drive each genuinely-working local-model harness over ACP (local only, -m integration).

These point a real ACP agent at a running local runtime (Ollama / LM Studio) through the SAME ``[agents.<id>]
base=<agent> backend=<backend> model=<m>`` config path a user writes, build the registry, and drive a real
turn -- the live proof that each supported ``(agent, backend)`` pair in ``roster._BACKEND_ENV`` answers.

What is and is NOT covered, vetted live 2026-06-14 (see docs/local-models.md for the full matrix):

- goose / qwen / opencode answer on BOTH Ollama and LM Studio (OpenAI-compatible ``/v1``; opencode via an
  inline ``OPENCODE_CONFIG_CONTENT`` provider block). These are parametrized below.
- claude_code answers on Ollama (Anthropic-compatible ``/v1/messages``) but is SLOW -- it needs a generous
  timeout and a capable model -- so it gets its own longer-budget test rather than the shared assertion.
- codex and hermes have NO env-keyed local pair (codex requires the OpenAI Responses API wire that local
  runtimes don't speak + is auth-gated; hermes' ``acp`` reads its config.yaml provider and ignores the env),
  so there is nothing to drive here -- they are documented, not tested.

Requires the runtime up with a tool-capable model loaded. A backend that is down skips its parametrizations.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

import pytest

from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.roster import build_registry
from rutherford.acp.session import run_acp_turn
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.enums import SafetyMode

pytestmark = pytest.mark.integration

_PROMPT = "Reply with ONLY the number, nothing else: what is 17 + 25?"

#: A tool-capable model each runtime serves on this machine (Ollama pulls ``qwen3:8b``; LM Studio serves
#: ``openai/gpt-oss-20b``). Swap for whatever the local box has loaded.
_OLLAMA_MODEL = "qwen3:8b"
_LMSTUDIO_MODEL = "openai/gpt-oss-20b"


def _runtime_up(url: str) -> bool:
    """Whether a local runtime answers its model-list endpoint within a short probe (else skip its cases)."""
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


_OLLAMA_UP = _runtime_up("http://localhost:11434/api/tags")
_LMSTUDIO_UP = _runtime_up("http://localhost:1234/v1/models")


async def _drive_local(
    base: str, backend: Literal["ollama", "lmstudio"], model: str, *, timeout_s: float = 240.0
) -> None:
    """Build the registry from a ``base``/``backend``/``model`` config entry and assert the live turn answers 42."""
    config = RutherfordConfig(
        auto_detect_local_models=False,
        agents={"local": AgentConfig(base=base, backend=backend, model=model)},
    )
    descriptor = build_registry(config).get("local")
    result = await run_acp_turn(
        descriptor, _PROMPT, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(Path.cwd()), timeout_s=timeout_s
    )
    assert result.ok is True, f"{base}+{backend} failed: {result.error}"
    assert "42" in result.text, f"{base}+{backend} answered {result.text!r}"


@pytest.mark.skipif(not _OLLAMA_UP, reason="Ollama is not running on :11434")
@pytest.mark.parametrize("base", ["goose", "qwen", "opencode"])
async def test_openai_compatible_harness_answers_on_ollama(base: str) -> None:
    """goose / qwen / opencode each drive Ollama's qwen3:8b over ACP through the config-keyed backend env."""
    await _drive_local(base, "ollama", _OLLAMA_MODEL)


@pytest.mark.skipif(not _LMSTUDIO_UP, reason="LM Studio server is not running on :1234")
@pytest.mark.parametrize("base", ["goose", "qwen", "opencode"])
async def test_openai_compatible_harness_answers_on_lmstudio(base: str) -> None:
    """goose / qwen / opencode each drive LM Studio's gpt-oss-20b over ACP through the config-keyed backend env."""
    await _drive_local(base, "lmstudio", _LMSTUDIO_MODEL)


@pytest.mark.skipif(not _OLLAMA_UP, reason="Ollama is not running on :11434")
async def test_claude_code_answers_on_ollama_with_a_generous_budget() -> None:
    """claude_code drives Ollama via the Anthropic-compatible /v1/messages endpoint -- correct but slow.

    The claude-agent-acp SDK runs a full agentic loop over the Anthropic wire, which a local model serves much
    more slowly than its native OpenAI path: a tight budget times out and a weak model can answer wrong. With a
    capable model (qwen3:8b) and a long budget it answers 42. Kept separate from the shared assertion so its
    larger timeout is explicit and it never makes the fast harnesses look slow.
    """
    await _drive_local("claude_code", "ollama", _OLLAMA_MODEL, timeout_s=360.0)
