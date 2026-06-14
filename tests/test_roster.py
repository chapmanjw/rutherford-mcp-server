# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for building the agent registry from built-in defaults plus ``[agents.<id>]`` config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rutherford.acp.descriptors import HIGH_FIDELITY
from rutherford.acp.roster import build_registry
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.errors import ConfigError


def test_no_config_is_the_builtin_roster() -> None:
    registry = build_registry(RutherfordConfig())
    assert len(registry) == len(HIGH_FIDELITY) == 16
    assert registry.get("goose").command == ("goose", "acp")


def test_override_a_builtin_agent() -> None:
    config = RutherfordConfig(
        agents={
            "goose": AgentConfig(
                command=["goose", "acp", "--verbose"], default_model="gpt-5.1", handshake_timeout_s=45.0
            )
        }
    )
    goose = build_registry(config).get("goose")
    assert goose.command == ("goose", "acp", "--verbose")
    assert goose.default_model == "gpt-5.1"
    assert goose.handshake_timeout_s == 45.0
    assert goose.display_name == "Goose"  # preserved from the built-in


def test_extra_args_appended_to_builtin_command() -> None:
    config = RutherfordConfig(agents={"goose": AgentConfig(extra_args=["--log", "debug"])})
    assert build_registry(config).get("goose").command == ("goose", "acp", "--log", "debug")


def test_env_overrides_flow_to_descriptor() -> None:
    config = RutherfordConfig(agents={"goose": AgentConfig(env={"GOOSE_PROVIDER": "openai"})})
    assert build_registry(config).get("goose").env_overrides == (("GOOSE_PROVIDER", "openai"),)


def test_disable_a_builtin_agent() -> None:
    registry = build_registry(RutherfordConfig(agents={"openhands": AgentConfig(enabled=False)}))
    assert not registry.has("openhands")
    assert len(registry) == 15


def test_define_a_new_agent() -> None:
    config = RutherfordConfig(
        agents={"my-agent": AgentConfig(command=["node", "./agent.js"], provider="acme", default_model="m1")}
    )
    agent = build_registry(config).get("my-agent")
    assert agent.command == ("node", "./agent.js")
    assert agent.provider == "acme"
    assert agent.default_model == "m1"
    assert agent.handshake_timeout_s == 30.0  # the default for a new agent
    assert agent.display_name == "my-agent"


def test_new_agent_without_command_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="has no 'command'"):
        build_registry(RutherfordConfig(agents={"broken": AgentConfig(default_model="m")}))


def test_enabled_agents_restricts_the_roster() -> None:
    config = RutherfordConfig(enabled_agents=["goose", "codex"])
    registry = build_registry(config)
    assert registry.ids() == ["codex", "goose"]


def test_enabled_agents_can_include_a_newly_defined_one() -> None:
    config = RutherfordConfig(
        enabled_agents=["goose", "extra"],
        agents={"extra": AgentConfig(command=["extra-acp"])},
    )
    assert build_registry(config).ids() == ["extra", "goose"]


def test_base_clones_a_builtin_launch() -> None:
    agent = build_registry(RutherfordConfig(agents={"my-goose": AgentConfig(base="goose")})).get("my-goose")
    assert agent.command == ("goose", "acp")
    assert agent.display_name == "Goose"


def test_backend_ollama_fills_goose_env() -> None:
    config = RutherfordConfig(agents={"local": AgentConfig(base="goose", backend="ollama", model="gemma3:12b")})
    agent = build_registry(config).get("local")
    assert agent.command == ("goose", "acp")
    assert dict(agent.env_overrides) == {
        "GOOSE_PROVIDER": "ollama",
        "GOOSE_MODEL": "gemma3:12b",
        "OLLAMA_HOST": "localhost:11434",
    }
    assert agent.provider == "ollama" and agent.default_model == "gemma3:12b"


def test_backend_lmstudio_uses_openai_compatible_env_and_host() -> None:
    config = RutherfordConfig(
        agents={"lm": AgentConfig(base="goose", backend="lmstudio", model="openai/gpt-oss-120b", host="localhost:4321")}
    )
    env = dict(build_registry(config).get("lm").env_overrides)
    assert env["GOOSE_PROVIDER"] == "openai" and env["GOOSE_MODEL"] == "openai/gpt-oss-120b"
    assert env["OPENAI_HOST"] == "http://localhost:4321" and env["OPENAI_BASE_PATH"] == "v1/chat/completions"
    assert build_registry(config).get("lm").provider == "lmstudio"


def test_explicit_env_overrides_the_backend_default() -> None:
    config = RutherfordConfig(
        agents={"local": AgentConfig(base="goose", backend="ollama", model="m", env={"OLLAMA_HOST": "box:9999"})}
    )
    assert dict(build_registry(config).get("local").env_overrides)["OLLAMA_HOST"] == "box:9999"


def test_backend_qwen_uses_openai_compatible_env() -> None:
    config = RutherfordConfig(agents={"q": AgentConfig(base="qwen", backend="ollama", model="m")})
    env = dict(build_registry(config).get("q").env_overrides)
    assert env == {"OPENAI_BASE_URL": "http://localhost:11434/v1", "OPENAI_API_KEY": "local", "OPENAI_MODEL": "m"}


def test_backend_qwen_lmstudio_points_at_1234() -> None:
    config = RutherfordConfig(agents={"q": AgentConfig(base="qwen", backend="lmstudio", model="m")})
    assert dict(build_registry(config).get("q").env_overrides)["OPENAI_BASE_URL"] == "http://localhost:1234/v1"


def test_backend_claude_code_uses_anthropic_compatible_env() -> None:
    config = RutherfordConfig(agents={"c": AgentConfig(base="claude_code", backend="ollama", model="m")})
    env = dict(build_registry(config).get("c").env_overrides)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434" and env["ANTHROPIC_MODEL"] == "m"


def test_backend_without_a_base_is_an_error() -> None:
    with pytest.raises(ConfigError, match="no 'base'"):
        build_registry(RutherfordConfig(agents={"floaty": AgentConfig(backend="ollama", model="m")}))


def test_unsupported_base_backend_pair_is_an_error() -> None:
    # a vendor-locked base has no local backend...
    with pytest.raises(ConfigError, match="does not support"):
        build_registry(RutherfordConfig(agents={"x": AgentConfig(base="cursor", backend="ollama", model="m")}))
    # ...and claude_code can't use LM Studio (OpenAI-only; claude_code needs an Anthropic-compatible endpoint)
    with pytest.raises(ConfigError, match="does not support"):
        build_registry(RutherfordConfig(agents={"y": AgentConfig(base="claude_code", backend="lmstudio", model="m")}))


def test_unknown_base_is_an_error() -> None:
    with pytest.raises(ConfigError, match="not a built-in"):
        build_registry(RutherfordConfig(agents={"x": AgentConfig(base="nope")}))


def test_backend_without_model_is_rejected_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        AgentConfig(base="goose", backend="ollama")
