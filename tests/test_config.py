# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for configuration loading, env overrides, and the schema helpers/validators."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.config.loader import deep_merge, default_global_config_path, has_project_config, load_config
from rutherford.config.schema import AgentConfig, RutherfordConfig
from rutherford.domain.enums import Effort, SafetyMode
from rutherford.domain.errors import ConfigError


def _iso_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose global-config roots point at an empty temp dir, so no real config leaks in."""
    return {"APPDATA": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}


def test_load_defaults(tmp_path: Path) -> None:
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path)
    assert config.default_safety_mode is SafetyMode.READ_ONLY
    assert config.default_timeout_s == 300.0


def test_load_project_override(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text(
        'default_timeout_s = 12.0\ndefault_safety_mode = "propose"\n\n[agents.goose]\ndefault_model = "gpt"\n'
        "timeout_s = 9.0\n",
        encoding="utf-8",
    )
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path)
    assert config.default_timeout_s == 12.0
    assert config.default_safety_mode is SafetyMode.PROPOSE
    assert config.default_model_for("goose") == "gpt"
    assert config.timeout_for("goose") == 9.0
    assert config.timeout_for("missing") is None


@pytest.mark.parametrize("name", ["rutherford.toml", ".rutherford.toml", ".rutherford/config.toml"])
def test_has_project_config_honors_every_name(name: str, tmp_path: Path) -> None:
    assert has_project_config(tmp_path) is False  # nothing yet
    cfg = tmp_path / name
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('default_safety_mode = "read_only"\n', encoding="utf-8")
    assert has_project_config(tmp_path) is True


def test_has_project_config_ignores_a_bare_rutherford_dir(tmp_path: Path) -> None:
    # A persisted run's ledger creates .rutherford/jobs/ but no config file -- that is NOT a configured workspace.
    (tmp_path / ".rutherford" / "jobs").mkdir(parents=True)
    assert has_project_config(tmp_path) is False


def test_load_explicit_and_missing(tmp_path: Path) -> None:
    target = tmp_path / "c.toml"
    target.write_text("max_depth = 5\n", encoding="utf-8")
    config = load_config(env=_iso_env(tmp_path), cwd=tmp_path, explicit_path=target)
    assert config.max_depth == 5
    with pytest.raises(ConfigError):
        load_config(env=_iso_env(tmp_path), cwd=tmp_path, explicit_path=tmp_path / "nope.toml")


def test_load_bad_toml_and_invalid_value(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env=_iso_env(tmp_path), cwd=tmp_path, explicit_path=bad)
    invalid = tmp_path / "inv.toml"
    invalid.write_text('default_safety_mode = "nonsense"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env=_iso_env(tmp_path), cwd=tmp_path, explicit_path=invalid)


def test_env_overrides(tmp_path: Path) -> None:
    env = _iso_env(tmp_path) | {
        "RUTHERFORD_MAX_DEPTH": "7",
        "RUTHERFORD_DEFAULT_TIMEOUT_S": "20",
        "RUTHERFORD_DEFAULT_SAFETY": "write",
        "RUTHERFORD_TRUSTED_WORKSPACES": str(tmp_path),
    }
    config = load_config(env=env, cwd=tmp_path)
    assert config.max_depth == 7
    assert config.default_timeout_s == 20.0
    assert config.default_safety_mode is SafetyMode.WRITE
    with pytest.raises(ConfigError):
        load_config(env=_iso_env(tmp_path) | {"RUTHERFORD_MAX_DEPTH": "notint"}, cwd=tmp_path)


def test_global_config_path_and_deep_merge() -> None:
    assert "rutherford" in str(default_global_config_path({"APPDATA": "C:/x"}))
    assert "rutherford" in str(default_global_config_path({"XDG_CONFIG_HOME": "/tmp"}))
    merged = deep_merge({"a": {"b": 1}}, {"a": {"c": 2}, "d": 3})
    assert merged == {"a": {"b": 1, "c": 2}, "d": 3}


def test_schema_helpers() -> None:
    config = RutherfordConfig(
        agents={"goose": AgentConfig(default_model="m", effort=Effort.HIGH, extra_args=["--x"])},
        default_effort=Effort.LOW,
    )
    assert config.effort_for("goose") is Effort.HIGH
    assert config.effort_for("other") is Effort.LOW
    assert config.extra_args_for("goose") == ["--x"]
    assert config.extra_args_for("other") == []
    assert config.wants_persist(True) is True
    assert config.wants_persist(None) is False
    assert RutherfordConfig(default_persistence="job").wants_persist(None) is True


def test_schema_concurrency_and_dir_resolution(tmp_path: Path) -> None:
    config = RutherfordConfig(max_targets=10, trusted_workspaces=[str(tmp_path)], role_dirs=[str(tmp_path / "missing")])
    assert config.max_concurrency == 10  # defaults to max_targets when not set
    assert config.trusted_workspaces[0] == str(tmp_path.resolve())
    assert RutherfordConfig(max_targets=10, max_concurrency=3).max_concurrency == 3
