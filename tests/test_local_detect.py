# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for zero-config auto-detection of local model backends (Ollama / LM Studio).

Every test fakes ``urllib.request.urlopen`` -- no test makes a live network call.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from rutherford.acp.local_detect import detect_local_agents
from rutherford.acp.roster import _goose_native, _goose_openai, build_registry
from rutherford.config.schema import AgentConfig, RutherfordConfig


class _FakeResponse:
    """A minimal stand-in for the object ``urlopen`` returns as a context manager."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _fake_urlopen(routes: dict[str, Any]) -> Any:
    """Build a fake ``urlopen`` that serves ``routes`` (url -> JSON body or an exception to raise).

    ``/api/show`` (a POST) is keyed by ``"show:<model>"`` so per-model capabilities can be faked.
    A url with no route raises ``URLError`` -- i.e. that endpoint is "down".
    """

    def urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url
        key = url
        if url.endswith("/api/show") and request.data is not None:
            model = json.loads(request.data)["model"]
            key = f"show:{model}"
        result = routes.get(key)
        if result is None:
            raise urllib.error.URLError(f"no route for {key}")
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(json.dumps(result).encode("utf-8"))

    return urlopen


@pytest.fixture
def patch_urlopen(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Yield a setter that installs a routed fake ``urlopen`` into the ``local_detect`` module."""

    def install(routes: dict[str, Any]) -> None:
        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen(routes))

    yield install


# ---- (a) Ollama: only tool-capable models register -------------------------------------------------


def test_ollama_registers_only_tool_models(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://localhost:11434/api/tags": {"models": [{"model": "qwen3:8b"}, {"model": "gemma3:12b"}]},
            "show:qwen3:8b": {"capabilities": ["completion", "tools"]},
            "show:gemma3:12b": {"capabilities": ["completion"]},  # no "tools" -> skipped
            # LM Studio not routed -> down -> contributes nothing
        }
    )
    agents = detect_local_agents()
    assert [a.id for a in agents] == ["ollama-qwen3-8b"]
    agent = agents[0]
    assert agent.display_name == "Ollama (qwen3:8b)"
    assert agent.command == ("goose", "acp")
    assert agent.provider == "ollama"
    assert agent.default_model == "qwen3:8b"
    assert dict(agent.env_overrides) == _goose_native("qwen3:8b", "localhost:11434")


# ---- (b) LM Studio: embedding models skipped -------------------------------------------------------


def test_lmstudio_registers_chat_models_skips_embeddings(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://localhost:1234/v1/models": {
                "data": [
                    {"id": "openai/gpt-oss-120b"},
                    {"id": "text-embedding-nomic-embed-text-v1.5"},  # contains "embed" -> skipped
                ]
            },
            # Ollama not routed -> down
        }
    )
    agents = detect_local_agents()
    assert [a.id for a in agents] == ["lmstudio-openai-gpt-oss-120b"]
    agent = agents[0]
    assert agent.display_name == "LM Studio (openai/gpt-oss-120b)"
    assert agent.provider == "lmstudio"
    assert agent.default_model == "openai/gpt-oss-120b"
    assert dict(agent.env_overrides) == _goose_openai("openai/gpt-oss-120b", "localhost:1234")


# ---- (c) both down -> [] ---------------------------------------------------------------------------


def test_both_backends_down_returns_empty(patch_urlopen: Any) -> None:
    patch_urlopen({})  # no routes -> every endpoint raises URLError
    assert detect_local_agents() == []


def test_timeout_on_a_backend_is_swallowed(patch_urlopen: Any) -> None:
    patch_urlopen({"http://localhost:11434/api/tags": TimeoutError("slow")})
    assert detect_local_agents() == []


def test_malformed_json_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        return _FakeResponse(b"not json{")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert detect_local_agents() == []


# ---- (d) ids are colon-free and env matches the single-source builders -----------------------------


def test_ids_are_colon_free_and_slugged(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://localhost:11434/api/tags": {"models": [{"model": "qwen3-coder:30b"}]},
            "show:qwen3-coder:30b": {"capabilities": ["tools"]},
            "http://localhost:1234/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
        }
    )
    ids = {a.id for a in detect_local_agents()}
    assert ids == {"ollama-qwen3-coder-30b", "lmstudio-openai-gpt-oss-20b"}
    assert all(":" not in agent_id for agent_id in ids)


def test_env_is_identical_to_a_manual_backend_entry(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://localhost:11434/api/tags": {"models": [{"model": "qwen3:8b"}]},
            "show:qwen3:8b": {"capabilities": ["tools"]},
            "http://localhost:1234/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
        }
    )
    detected = {a.provider: dict(a.env_overrides) for a in detect_local_agents()}

    manual_ollama = build_registry(
        RutherfordConfig(
            auto_detect_local_models=False,
            agents={"m": AgentConfig(base="goose", backend="ollama", model="qwen3:8b")},
        )
    ).get("m")
    manual_lmstudio = build_registry(
        RutherfordConfig(
            auto_detect_local_models=False,
            agents={"m": AgentConfig(base="goose", backend="lmstudio", model="openai/gpt-oss-20b")},
        )
    ).get("m")

    assert detected["ollama"] == dict(manual_ollama.env_overrides)
    assert detected["lmstudio"] == dict(manual_lmstudio.env_overrides)


def test_custom_hosts_thread_through(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://box:9999/api/tags": {"models": [{"model": "m"}]},
            "show:m": {"capabilities": ["tools"]},
        }
    )
    agents = detect_local_agents(ollama_host="box:9999", lmstudio_host="box:8888")
    assert dict(agents[0].env_overrides) == _goose_native("m", "box:9999")


# ---- build_registry wiring: detected agents appear, never override a built-in/explicit id ----------


def test_build_registry_appends_detected_agents(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://localhost:11434/api/tags": {"models": [{"model": "qwen3:8b"}]},
            "show:qwen3:8b": {"capabilities": ["tools"]},
        }
    )
    registry = build_registry(RutherfordConfig())
    assert registry.has("ollama-qwen3-8b")
    assert registry.has("goose")  # built-ins still present


def test_auto_detect_off_skips_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> None:  # pragma: no cover - must never be called
        raise AssertionError("detection must not run when auto_detect_local_models is False")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    registry = build_registry(RutherfordConfig(auto_detect_local_models=False))
    assert not any(rid.startswith(("ollama-", "lmstudio-")) for rid in registry.ids())


def test_detected_never_overrides_a_builtin_of_the_same_slug(patch_urlopen: Any) -> None:
    # A detected model whose slug collides with the built-in ``goose`` id must NOT replace it.
    patch_urlopen(
        {
            "http://localhost:1234/v1/models": {"data": [{"id": "goose"}]},  # slug -> "lmstudio-goose"
            "http://localhost:11434/api/tags": {"models": [{"model": "goose"}]},
            "show:goose": {"capabilities": ["tools"]},
        }
    )
    registry = build_registry(RutherfordConfig())
    assert registry.get("goose").command == ("goose", "acp")  # the built-in, untouched
    assert registry.get("goose").provider is None  # not the detected provider
    assert registry.has("ollama-goose")  # the detected one lands under its slugged id


def test_explicit_config_wins_over_a_detected_agent(patch_urlopen: Any) -> None:
    patch_urlopen(
        {
            "http://localhost:11434/api/tags": {"models": [{"model": "qwen3:8b"}]},
            "show:qwen3:8b": {"capabilities": ["tools"]},
        }
    )
    config = RutherfordConfig(agents={"ollama-qwen3-8b": AgentConfig(command=["my-acp"], default_model="pinned")})
    agent = build_registry(config).get("ollama-qwen3-8b")
    assert agent.command == ("my-acp",)  # explicit config replaced the detected descriptor
    assert agent.default_model == "pinned"
