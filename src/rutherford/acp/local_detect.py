# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Zero-config auto-detection of local model backends (Ollama / LM Studio).

A manual ``[agents.<id>] base=goose backend=ollama model=X`` entry makes one local model a voice
(see :mod:`rutherford.acp.roster`). This module is the zero-config counterpart: probe a running
runtime over its HTTP API and turn every suitable model into a ready-to-run ``goose``-based ACP
agent, so a user who has Ollama or LM Studio up gets local voices with no config at all.

Probing is sync, bounded by a short timeout, and never raises: a backend that is down, slow, or
malformed contributes nothing and is silently skipped, so detection cannot break registry build.
The env each detected agent carries is produced by the SAME builders a manual entry uses
(:func:`rutherford.acp.roster._goose_native` / :func:`~rutherford.acp.roster._goose_openai`), so
there is one source of truth for how goose is pointed at a local runtime.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.request import Request

from .descriptors import AgentDescriptor
from .roster import _goose_native, _goose_openai

_log = logging.getLogger(__name__)

#: The argv that launches goose as an ACP server (mirrors the built-in ``goose`` descriptor).
_GOOSE_COMMAND: tuple[str, str] = ("goose", "acp")
#: goose's handshake budget: the default the built-in ``goose`` descriptor uses.
_GOOSE_HANDSHAKE_TIMEOUT_S = 30.0

#: Characters in a model id that must not survive into an agent id: a colon breaks ``cli:model``
#: target parsing (``partition(":")``); slashes and whitespace are slugged for the same readability.
_SLUG_RE = re.compile(r"[:/\s]+")


def detect_local_agents(
    *,
    ollama_host: str = "localhost:11434",
    lmstudio_host: str = "localhost:1234",
    timeout_s: float = 1.5,
) -> list[AgentDescriptor]:
    """Probe local runtimes and return a ``goose``-based ACP agent for every suitable model.

    Ollama models are included only when their ``capabilities`` report ``"tools"`` (an agentic ACP
    loop needs tool-calling; a non-tool model like ``gemma3:12b`` fails the turn). LM Studio does not
    expose tool capability, so every non-embedding model id is included (ids containing ``"embed"``
    are skipped). A backend that is unreachable, slow, or returns garbage contributes nothing -- this
    never raises, so it is safe to call at registry-build time.
    """
    agents: list[AgentDescriptor] = []
    agents.extend(_detect_ollama(ollama_host, timeout_s))
    agents.extend(_detect_lmstudio(lmstudio_host, timeout_s))
    return agents


def _detect_ollama(host: str, timeout_s: float) -> list[AgentDescriptor]:
    """Detect tool-capable Ollama models as goose agents; return ``[]`` if Ollama is down."""
    tags = _get_json(f"http://{host}/api/tags", timeout_s)
    if not isinstance(tags, dict):
        return []
    agents: list[AgentDescriptor] = []
    for entry in tags.get("models", []):
        name = entry.get("model") or entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            continue
        if not _ollama_supports_tools(host, name, timeout_s):
            continue
        agents.append(
            _descriptor(
                provider="ollama",
                model=name,
                env=_goose_native(name, host),
            )
        )
    return agents


def _ollama_supports_tools(host: str, model: str, timeout_s: float) -> bool:
    """Whether Ollama reports ``"tools"`` in this model's capabilities (``POST /api/show``)."""
    payload = json.dumps({"model": model}).encode("utf-8")
    show = _get_json(f"http://{host}/api/show", timeout_s, data=payload)
    if not isinstance(show, dict):
        return False
    capabilities = show.get("capabilities")
    return isinstance(capabilities, list) and "tools" in capabilities


def _detect_lmstudio(host: str, timeout_s: float) -> list[AgentDescriptor]:
    """Detect non-embedding LM Studio models as goose agents; return ``[]`` if LM Studio is down."""
    models = _get_json(f"http://{host}/v1/models", timeout_s)
    if not isinstance(models, dict):
        return []
    agents: list[AgentDescriptor] = []
    for entry in models.get("data", []):
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(model_id, str) or not model_id:
            continue
        if "embed" in model_id:  # an embedding model is not a chat/agent model
            continue
        agents.append(
            _descriptor(
                provider="lmstudio",
                model=model_id,
                env=_goose_openai(model_id, host),
            )
        )
    return agents


def _descriptor(*, provider: str, model: str, env: dict[str, str]) -> AgentDescriptor:
    """Build the goose-based ACP descriptor for one detected local model.

    The id is colon-free (``ollama-gemma3-12b``) so ``cli:model`` target parsing is unbroken; the
    display name keeps the readable model id (``Ollama (gemma3:12b)``).
    """
    label = "Ollama" if provider == "ollama" else "LM Studio"
    return AgentDescriptor(
        id=f"{provider}-{_slug(model)}",
        display_name=f"{label} ({model})",
        command=_GOOSE_COMMAND,
        provider=provider,
        default_model=model,
        handshake_timeout_s=_GOOSE_HANDSHAKE_TIMEOUT_S,
        env_overrides=tuple(env.items()),
    )


def _slug(model: str) -> str:
    """Slug a model id into a colon-free agent-id fragment (``gemma3:12b`` -> ``gemma3-12b``)."""
    return _SLUG_RE.sub("-", model.strip()).strip("-")


def _get_json(url: str, timeout_s: float, *, data: bytes | None = None) -> Any:
    """Fetch and parse JSON from a local runtime; return ``None`` on any failure (never raises).

    A ``data`` body makes it a POST with a JSON content type. Any network error, timeout, non-200,
    or unparseable body is swallowed and logged at debug -- a down backend is an expected state, not
    an error, so detection degrades to "no agents from here".
    """
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.debug("local-detect: %s unreachable (%s)", url, exc)
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.debug("local-detect: %s returned non-JSON (%s)", url, exc)
        return None
