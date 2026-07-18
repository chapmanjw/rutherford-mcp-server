# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the global ``trust`` / ``untrust`` allowlist helpers and CLI."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.config.loader import default_global_config_path, load_config
from rutherford.config.trust import (
    read_global_trusted_workspaces,
    trust_workspace,
    untrust_workspace,
)
from rutherford.domain.errors import ConfigError


def _redirect_global(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point the platform global config dir at ``tmp_path`` on every OS."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return default_global_config_path()


def test_trust_creates_global_config_with_cwd(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)

    result = trust_workspace()

    assert result.action == "added"
    assert result.workspace == str(work.resolve())
    assert config_path.exists()
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["trusted_workspaces"] == [str(work.resolve())]


def test_trust_is_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()

    first = trust_workspace(work)
    second = trust_workspace(work)

    assert first.action == "added"
    assert second.action == "unchanged"
    assert second.note is not None
    _, listed = read_global_trusted_workspaces()
    assert listed == [str(work.resolve())]


def test_trust_appends_without_clobbering_agents(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'default_timeout_s = 90\n\n[agents.fake]\ncommand = ["fake-acp"]\n',
        encoding="utf-8",
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    text = config_path.read_text(encoding="utf-8")
    assert 'command = ["fake-acp"]' in text
    assert "default_timeout_s = 90" in text
    parsed = tomllib.loads(text)
    assert parsed["trusted_workspaces"] == [str(work.resolve())]
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]


def test_trust_replaces_an_existing_multiline_assignment(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f'trusted_workspaces = [\n    "{other.as_posix()}",\n]\nlog_level = "info"\n',
        encoding="utf-8",
    )
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert set(parsed["trusted_workspaces"]) == {str(other.resolve()), str(work.resolve())}
    assert parsed["log_level"] == "info"
    # * Only one assignment remains in the file text.
    assert config_path.read_text(encoding="utf-8").count("trusted_workspaces") == 1


def test_untrust_removes_cwd(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    trust_workspace(work)

    result = untrust_workspace(work)

    assert result.action == "removed"
    assert result.trusted_workspaces == ()
    _, listed = read_global_trusted_workspaces()
    assert listed == []


def test_untrust_missing_is_idempotent(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()

    result = untrust_workspace(work)

    assert result.action == "missing"
    assert not default_global_config_path().exists()


def test_malformed_global_config_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("trusted_workspaces = [\n", encoding="utf-8")
    work = tmp_path / "repo"
    work.mkdir()

    with pytest.raises(ConfigError, match="not valid TOML"):
        trust_workspace(work)


def test_trusted_path_is_honored_by_load_config(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    trust_workspace()

    config = load_config(cwd=work)
    assert str(work.resolve()) in config.trusted_workspaces


def test_trust_cli_and_list(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)

    server._trust_cli([])
    out = capsys.readouterr().out
    assert "added" in out and str(work.resolve()) in out

    server._trust_cli(["--list"])
    listed = capsys.readouterr().out
    assert "trusted_workspaces (1):" in listed
    assert str(work.resolve()) in listed

    server._untrust_cli([])
    removed = capsys.readouterr().out
    assert "removed" in removed


def test_trust_cli_rejects_extra_args(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        server._trust_cli(["a", "b"])
    assert exc.value.code == 2


def test_trust_cli_rejects_unknown_flags(tmp_path: Path, monkeypatch: Any) -> None:
    _redirect_global(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        server._trust_cli(["--global"])
    assert exc.value.code == 2


def test_trust_inserts_before_leading_agents_table(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = _redirect_global(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[agents.fake]\ncommand = ["fake-acp"]\n', encoding="utf-8")
    work = tmp_path / "repo"
    work.mkdir()

    trust_workspace(work)

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["trusted_workspaces"] == [str(work.resolve())]
    assert parsed["agents"]["fake"]["command"] == ["fake-acp"]
    # * The allowlist assignment must appear before the first table header in the file text.
    text = config_path.read_text(encoding="utf-8")
    assert text.index("trusted_workspaces") < text.index("[agents.fake]")
