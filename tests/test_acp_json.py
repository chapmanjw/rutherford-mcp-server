# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for importing a Zed/Cline acp.json agent_servers block as Rutherford agent config."""

from __future__ import annotations

import json
from pathlib import Path

from rutherford.acp.roster import build_registry
from rutherford.config.acp_json import agents_from_acp_json
from rutherford.config.loader import load_config


def _iso_env(tmp_path: Path) -> dict[str, str]:
    return {"APPDATA": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}


def test_parse_agent_servers() -> None:
    agents = agents_from_acp_json(
        {
            "agent_servers": {
                "Cline": {"command": "cline", "args": ["--acp"], "env": {}},
                "Custom Agent": {"command": "node", "args": ["agent.js"], "env": {"TOKEN": "x"}},
            }
        }
    )
    assert agents["cline"].command == ["cline", "--acp"]
    assert agents["custom_agent"].command == ["node", "agent.js"]
    assert agents["custom_agent"].env == {"TOKEN": "x"}


def test_parse_is_tolerant_of_malformed_entries() -> None:
    assert agents_from_acp_json({}) == {}
    assert agents_from_acp_json({"agent_servers": "nope"}) == {}
    agents = agents_from_acp_json(
        {
            "agent_servers": {
                "ok": {"command": "x"},
                "no_command": {"args": ["y"]},  # skipped: nothing to launch
                "not_a_map": 123,  # skipped
            }
        }
    )
    assert list(agents) == ["ok"]
    assert agents["ok"].command == ["x"]


def test_loader_discovers_project_acp_json(tmp_path: Path) -> None:
    acp_dir = tmp_path / ".rutherford"
    acp_dir.mkdir()
    (acp_dir / "acp.json").write_text(
        json.dumps({"agent_servers": {"My Agent": {"command": "node", "args": ["agent.js"], "env": {"K": "v"}}}}),
        encoding="utf-8",
    )
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path)
    assert config.agents["my_agent"].command == ["node", "agent.js"]
    assert config.agents["my_agent"].env == {"K": "v"}
    # ...and it reaches the registry as a launchable agent.
    registry = build_registry(config)
    assert registry.get("my_agent").command == ("node", "agent.js")


def test_loader_tolerates_a_malformed_acp_json(tmp_path: Path) -> None:
    acp_dir = tmp_path / ".rutherford"
    acp_dir.mkdir()
    (acp_dir / "acp.json").write_text("{ this is not valid json", encoding="utf-8")
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path)  # must not raise; the import is best-effort
    assert config.agents == {}


def test_imported_acp_json_does_not_clobber_a_builtin(tmp_path: Path) -> None:
    acp_dir = tmp_path / ".rutherford"
    acp_dir.mkdir()
    (acp_dir / "acp.json").write_text(
        json.dumps({"agent_servers": {"codex": {"command": "weird", "args": ["x"]}}}), encoding="utf-8"
    )
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path)
    assert "codex" not in config.agents  # the built-in id is skipped, not overridden
    assert build_registry(config).get("codex").command == ("codex-acp",)  # curated launch preserved


def test_toml_agents_win_over_imported_acp_json(tmp_path: Path) -> None:
    acp_dir = tmp_path / ".rutherford"
    acp_dir.mkdir()
    (acp_dir / "acp.json").write_text(json.dumps({"agent_servers": {"dup": {"command": "from-acp"}}}), encoding="utf-8")
    (tmp_path / "rutherford.toml").write_text('[agents.dup]\ncommand = ["from-toml"]\n', encoding="utf-8")
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path)
    assert config.agents["dup"].command == ["from-toml"]
