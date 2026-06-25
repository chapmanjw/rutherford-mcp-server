# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for Bedrock/Vertex Claude Code model-env normalization (acp/host_env.py)."""

from __future__ import annotations

import json
from pathlib import Path

from rutherford.acp.descriptors import AgentDescriptor
from rutherford.acp.host_env import claude_bedrock_env

CLAUDE = AgentDescriptor(
    "claude_code",
    "Claude Code",
    ("claude-agent-acp",),
    provider="anthropic",
    underlying_cli="claude",
    adapter_package="@agentclientprotocol/claude-agent-acp",
)
CODEX = AgentDescriptor("codex", "Codex", ("codex-acp",), provider="openai", underlying_cli="codex")
BEDROCK = {"CLAUDE_CODE_USE_BEDROCK": "1"}
OPUS = "us.anthropic.claude-opus-4-1-20250805-v1:0"


def _write_settings(home: Path, env_block: dict[str, str]) -> Path:
    """Write a Claude Code settings.json with ``env_block`` under ``<home>/.claude`` and return ``home``."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"env": env_block}), encoding="utf-8")
    return home


def test_no_op_without_a_bedrock_or_vertex_flag(tmp_path: Path) -> None:
    # No flag -> a clean no-op even with a model id available, so a normal API-key seat is untouched.
    env = {"ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS}
    assert claude_bedrock_env(CLAUDE, env, str(tmp_path)) == {}


def test_falsey_flag_is_off(tmp_path: Path) -> None:
    env = {"CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS}
    assert claude_bedrock_env(CLAUDE, env, str(tmp_path)) == {}


def test_no_op_for_a_non_claude_seat(tmp_path: Path) -> None:
    # The fix is scoped to the Claude Code adapter; another seat is never touched even on a Bedrock host.
    env = {**BEDROCK, "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS}
    assert claude_bedrock_env(CODEX, env, str(tmp_path)) == {}


def test_promotes_env_default_opus_model(tmp_path: Path) -> None:
    # The confirmed real case: ANTHROPIC_DEFAULT_OPUS_MODEL is set but the adapter never reads it AS the model,
    # so Rutherford promotes it to ANTHROPIC_MODEL (which the adapter/SDK does use).
    env = {**BEDROCK, "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS}
    assert claude_bedrock_env(CLAUDE, env, str(tmp_path)) == {"ANTHROPIC_MODEL": OPUS}


def test_existing_anthropic_model_is_kept_not_clobbered(tmp_path: Path) -> None:
    env = {**BEDROCK, "ANTHROPIC_MODEL": OPUS, "ANTHROPIC_DEFAULT_OPUS_MODEL": "other:0"}
    assert claude_bedrock_env(CLAUDE, env, str(tmp_path)) == {"ANTHROPIC_MODEL": OPUS}


def test_reads_anthropic_model_from_user_settings_json(tmp_path: Path) -> None:
    home = _write_settings(tmp_path / "home", {"ANTHROPIC_MODEL": OPUS})
    env = {**BEDROCK, "USERPROFILE": str(home), "HOME": str(home)}
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_bedrock_env(CLAUDE, env, str(cwd)) == {"ANTHROPIC_MODEL": OPUS}


def test_promotes_settings_default_opus_and_haiku(tmp_path: Path) -> None:
    haiku = "us.anthropic.claude-haiku-4-5-v1:0"
    home = _write_settings(
        tmp_path / "home", {"ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS, "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku}
    )
    env = {**BEDROCK, "USERPROFILE": str(home), "HOME": str(home)}
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_bedrock_env(CLAUDE, env, str(cwd)) == {"ANTHROPIC_MODEL": OPUS, "ANTHROPIC_SMALL_FAST_MODEL": haiku}


def test_project_settings_override_user_settings(tmp_path: Path) -> None:
    home = _write_settings(tmp_path / "home", {"ANTHROPIC_MODEL": "us.anthropic.user:0"})
    cwd = _write_settings(tmp_path / "proj", {"ANTHROPIC_MODEL": "us.anthropic.project:0"})
    env = {**BEDROCK, "USERPROFILE": str(home), "HOME": str(home)}
    assert claude_bedrock_env(CLAUDE, env, str(cwd)) == {"ANTHROPIC_MODEL": "us.anthropic.project:0"}


def test_config_alias_default_model_is_not_promoted(tmp_path: Path) -> None:
    # A [agents.claude_code] model that is a Claude Code ALIAS (sonnet/haiku/default) must NOT be promoted to
    # ANTHROPIC_MODEL -- doing so would recreate the bug. It is skipped; resolution falls through to settings.
    seat = AgentDescriptor(
        "claude_code",
        "Claude Code",
        ("claude-agent-acp",),
        provider="anthropic",
        underlying_cli="claude",
        default_model="sonnet",
    )
    home = _write_settings(tmp_path / "home", {"ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS})
    env = {**BEDROCK, "USERPROFILE": str(home), "HOME": str(home)}
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_bedrock_env(seat, env, str(cwd)) == {"ANTHROPIC_MODEL": OPUS}


def test_config_raw_provider_default_model_is_promoted(tmp_path: Path) -> None:
    seat = AgentDescriptor(
        "claude_code",
        "Claude Code",
        ("claude-agent-acp",),
        provider="anthropic",
        underlying_cli="claude",
        default_model=OPUS,
    )
    assert claude_bedrock_env(seat, {**BEDROCK}, str(tmp_path)) == {"ANTHROPIC_MODEL": OPUS}


def test_no_resolvable_id_returns_empty(tmp_path: Path) -> None:
    # Bedrock host but the model id is nowhere -> {} (the turn still fails, but doctor then says model_unavailable
    # with guidance rather than silently inventing an id).
    env = {**BEDROCK, "USERPROFILE": str(tmp_path), "HOME": str(tmp_path)}
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_bedrock_env(CLAUDE, env, str(cwd)) == {}


def test_malformed_settings_json_does_not_crash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{ this is not valid json", encoding="utf-8")
    env = {**BEDROCK, "USERPROFILE": str(home), "HOME": str(home)}
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert claude_bedrock_env(CLAUDE, env, str(cwd)) == {}  # tolerated: no id resolved, no exception


def test_vertex_flag_also_triggers_resolution(tmp_path: Path) -> None:
    vertex_id = "claude-opus-4-1@20250805"
    env = {"CLAUDE_CODE_USE_VERTEX": "true", "ANTHROPIC_DEFAULT_OPUS_MODEL": vertex_id}
    assert claude_bedrock_env(CLAUDE, env, str(tmp_path)) == {"ANTHROPIC_MODEL": vertex_id}


def test_raw_command_override_is_not_treated_as_the_claude_adapter(tmp_path: Path) -> None:
    # A raw command override of the claude_code id launches a custom server, so the roster drops its
    # underlying_cli -- and the gate must NOT inject Anthropic-specific env into it just because the id matches.
    override = AgentDescriptor("claude_code", "Claude Code", ("my-own-acp",), provider="anthropic")
    env = {**BEDROCK, "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS}
    assert claude_bedrock_env(override, env, str(tmp_path)) == {}


def test_renamed_claude_seat_is_recognized_by_underlying_cli(tmp_path: Path) -> None:
    # A cloned/renamed Claude Code seat (id != "claude_code") that still carries underlying_cli == "claude"
    # (preserved by the roster merge) is recognized and normalized too -- the gate is not just the id.
    renamed = AgentDescriptor(
        "bedrock_claude", "Bedrock Claude", ("claude-agent-acp",), provider="anthropic", underlying_cli="claude"
    )
    env = {**BEDROCK, "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS}
    assert claude_bedrock_env(renamed, env, str(tmp_path)) == {"ANTHROPIC_MODEL": OPUS}
