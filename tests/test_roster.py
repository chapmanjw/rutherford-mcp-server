# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for building the agent registry from built-in defaults plus ``[agents.<id>]`` config."""

from __future__ import annotations

import pytest

from rutherford.acp.descriptors import HIGH_FIDELITY
from rutherford.acp.roster import build_registry
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.errors import ConfigError


def test_no_config_is_the_builtin_roster() -> None:
    registry = build_registry(RutherfordConfig())
    assert len(registry) == len(HIGH_FIDELITY) == 14
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
    assert len(registry) == 13


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
