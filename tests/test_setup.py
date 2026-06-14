# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the ``setup`` first-run helper: path resolution, the starter TOML, write/no-clobber, scopes."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.loader import default_global_config_path
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import decode
from rutherford.tools.setup import setup_tool

FAKE = AgentDescriptor("fake", "Fake", ("fake-acp",))
OTHER = AgentDescriptor("other", "Other", ("other-acp",))


def _app(config: RutherfordConfig | None = None) -> AppContext:
    return build_app_context(
        config=config or RutherfordConfig(),
        descriptors=DescriptorRegistry([FAKE, OTHER]),
    )


def _resolved(path: str) -> str:
    """Resolve a path string in a sync helper, so an async test body never calls a Path method (ASYNC240)."""
    return str(Path(path).resolve())


async def test_write_false_returns_content_without_a_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    data = decode(await setup_tool(_app(), scope="project"))
    assert data["written"] is False
    assert data["already_exists"] is False
    assert data["content"]  # a non-empty starter scaffold is returned
    target = tmp_path / ".rutherford" / "config.toml"
    assert data["path"] == str(target)
    assert not target.exists()  # nothing written


async def test_write_true_creates_project_config_with_valid_toml(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    data = decode(await setup_tool(_app(), scope="project", write=True))
    target = tmp_path / ".rutherford" / "config.toml"
    assert data["written"] is True
    assert data["exists"] is False  # it did not exist before this call
    assert target.exists()
    parsed = tomllib.loads(target.read_text(encoding="utf-8"))  # round-trips through tomllib
    assert parsed["default_safety_mode"] == "read_only"


async def test_second_write_does_not_clobber(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".rutherford" / "config.toml"
    await setup_tool(_app(), scope="project", write=True)
    original = target.read_text(encoding="utf-8")
    # A user-edited file must survive a re-run.
    target.write_text(original + "\nmax_targets = 3\n", encoding="utf-8")
    edited = target.read_text(encoding="utf-8")
    data = decode(await setup_tool(_app(), scope="project", write=True))
    assert data["written"] is False
    assert data["already_exists"] is True
    assert target.read_text(encoding="utf-8") == edited  # untouched


async def test_global_scope_targets_the_global_path(tmp_path, monkeypatch) -> None:
    # Redirect the global config dir to tmp_path on every platform so nothing is written to the real home.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    data = decode(await setup_tool(_app(), scope="global"))
    assert data["scope"] == "global"
    assert data["path"] == str(default_global_config_path())
    assert str(tmp_path) in data["path"]


async def test_invalid_scope_raises_invalid_input() -> None:
    with pytest.raises(RutherfordError) as exc:
        await setup_tool(_app(), scope="user")
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert "global" in exc.value.message and "project" in exc.value.message


async def test_trust_workspace_puts_cwd_into_trusted_workspaces(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    await setup_tool(_app(), scope="project", write=True, trust_workspace=True)
    target = tmp_path / ".rutherford" / "config.toml"
    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    trusted = parsed["trusted_workspaces"]
    assert isinstance(trusted, list) and len(trusted) == 1
    # The written cwd matches tmp_path; compare resolved forms (a sync helper avoids ASYNC240).
    assert _resolved(trusted[0]) == _resolved(str(tmp_path))


async def test_roster_snapshot_reports_registered_agents(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    data = decode(await setup_tool(_app(), scope="project"))
    assert data["agent_count"] == 2
    assert data["agents"] == ["fake", "other"]  # sorted ids


async def test_generated_toml_parses_and_validates_against_config(tmp_path, monkeypatch) -> None:
    # The strongest guard: the scaffold must parse AND validate with no invalid keys (extra="forbid").
    monkeypatch.chdir(tmp_path)
    for trust in (False, True):
        data = decode(await setup_tool(_app(), scope="project", trust_workspace=trust))
        parsed = tomllib.loads(data["content"])
        RutherfordConfig.model_validate(parsed)  # raises on an unknown or invalid key


async def test_starter_reflects_effective_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = RutherfordConfig(default_timeout_s=120.0, max_targets=5, auto_detect_local_models=False)
    data = decode(await setup_tool(_app(config), scope="project"))
    parsed = tomllib.loads(data["content"])
    assert parsed["default_timeout_s"] == 120
    assert parsed["max_targets"] == 5
    assert parsed["auto_detect_local_models"] is False
