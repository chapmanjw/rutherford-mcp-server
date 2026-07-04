# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for building the agent registry from built-in defaults plus ``[agents.<id>]`` config."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rutherford.acp.descriptors import HIGH_FIDELITY
from rutherford.acp.roster import build_registry
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.errors import ConfigError


def test_no_config_is_the_builtin_roster() -> None:
    # auto_detect_local_models off so the count is deterministic regardless of a running local backend.
    registry = build_registry(RutherfordConfig(auto_detect_local_models=False))
    assert len(registry) == len(HIGH_FIDELITY) == 20
    assert registry.get("goose").command == ("goose", "acp")
    assert registry.get("gemini").command == ("gemini", "--acp")
    assert registry.get("qoder").command == ("qodercli", "--acp")
    assert registry.get("grok").command == ("grok", "agent", "stdio")
    assert registry.get("fast_agent").command == ("uvx", "fast-agent-acp==0.8.3")


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
    registry = build_registry(
        RutherfordConfig(auto_detect_local_models=False, agents={"openhands": AgentConfig(enabled=False)})
    )
    assert not registry.has("openhands")
    assert len(registry) == len(HIGH_FIDELITY) - 1


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


def test_base_clone_inherits_wrapped_adapter_identity() -> None:
    # A renamed clone of a wrapped-adapter built-in (claude_code -> claude-agent-acp) keeps its underlying_cli
    # and adapter_package, so it is still recognized as that adapter -- by doctor's install hint AND the
    # Bedrock/Vertex model-env normalization gate (which keys off underlying_cli == "claude").
    agent = build_registry(RutherfordConfig(agents={"bedrock_claude": AgentConfig(base="claude_code")})).get(
        "bedrock_claude"
    )
    assert agent.command == ("claude-agent-acp",)
    assert agent.underlying_cli == "claude"
    assert agent.adapter_package == "@agentclientprotocol/claude-agent-acp"
    assert agent.is_wrapped_adapter is True


def test_same_id_override_preserves_wrapped_adapter_identity() -> None:
    # Overriding a built-in in place (no raw command) must not strip its adapter identity.
    agent = build_registry(RutherfordConfig(agents={"claude_code": AgentConfig(default_model="sonnet")})).get(
        "claude_code"
    )
    assert agent.underlying_cli == "claude" and agent.is_wrapped_adapter is True


def test_raw_command_override_drops_adapter_identity() -> None:
    # A raw command override launches something else, so it must NOT inherit the built-in's adapter identity.
    agent = build_registry(RutherfordConfig(agents={"claude_code": AgentConfig(command=["my-own-acp"])})).get(
        "claude_code"
    )
    assert agent.command == ("my-own-acp",)
    assert agent.underlying_cli is None and agent.is_wrapped_adapter is False


def test_clone_records_effort_lineage() -> None:
    # effort_base lets effort dispatch follow the launched adapter, not the new config id. It is stamped only
    # when the clone inherits a built-in's launch command (no explicit command=); a raw command= agent gets
    # None (no knowable knob), and a same-id override gets its own id.
    config = RutherfordConfig(
        auto_detect_local_models=False,
        agents={
            "my-codex": AgentConfig(base="codex"),
            "my-goose": AgentConfig(base="goose"),
            "raw": AgentConfig(command=["raw-acp"]),
            "codex": AgentConfig(default_model="gpt-5.2"),
        },
    )
    registry = build_registry(config)
    assert registry.get("my-codex").effort_base == "codex"  # base clone -> base id
    assert registry.get("my-goose").effort_base == "goose"  # knob-less base, still stamped (resolves to no-op)
    assert registry.get("raw").effort_base is None  # raw command= -> no lineage
    assert registry.get("codex").effort_base == "codex"  # same-id override -> its own id
    # Built-ins themselves never carry a lineage.
    assert all(descriptor.effort_base is None for descriptor in HIGH_FIDELITY)


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


def test_backend_opencode_ollama_builds_inline_config_env() -> None:
    config = RutherfordConfig(agents={"oc": AgentConfig(base="opencode", backend="ollama", model="qwen3:8b")})
    agent = build_registry(config).get("oc")
    assert agent.command == ("opencode", "acp")
    env = dict(agent.env_overrides)
    assert set(env) == {"OPENCODE_CONFIG_CONTENT"}  # opencode is configured entirely through one inline-JSON env
    config_json = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    provider = config_json["provider"]["ollama"]  # provider id is the backend name
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://localhost:11434/v1"
    assert "qwen3:8b" in provider["models"]  # the requested model is opencode's single default model


def test_backend_opencode_lmstudio_points_at_1234_and_names_lmstudio_provider() -> None:
    config = RutherfordConfig(
        agents={"oc": AgentConfig(base="opencode", backend="lmstudio", model="openai/gpt-oss-20b")}
    )
    env = dict(build_registry(config).get("oc").env_overrides)
    config_json = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    provider = config_json["provider"]["lmstudio"]  # LM Studio gets an lmstudio-named provider block
    assert provider["options"]["baseURL"] == "http://localhost:1234/v1"
    assert "openai/gpt-oss-20b" in provider["models"]


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
