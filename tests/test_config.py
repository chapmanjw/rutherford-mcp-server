# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the configuration schema and the global + project-local loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rutherford.config.loader import deep_merge, load_config
from rutherford.config.schema import AdapterConfig, RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.errors import ConfigError


def _env(global_root: Path) -> dict[str, str]:
    """An env that points both the Windows and XDG global-config roots at a temp dir."""
    return {"APPDATA": str(global_root), "XDG_CONFIG_HOME": str(global_root)}


def test_defaults_when_no_files(tmp_path: Path) -> None:
    config = load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)
    assert config.default_safety_mode is SafetyMode.READ_ONLY
    assert config.max_depth == 3
    assert config.max_targets == 8
    assert config.default_timeout_s == 300.0


def test_project_override(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text("max_depth = 5\n", encoding="utf-8")
    config = load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)
    assert config.max_depth == 5


def test_global_and_project_merge(tmp_path: Path) -> None:
    global_root = tmp_path / "globalroot"
    global_dir = global_root / "rutherford"
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text("default_timeout_s = 100.0\nmax_depth = 2\n", encoding="utf-8")
    (tmp_path / "rutherford.toml").write_text("max_depth = 7\n", encoding="utf-8")

    config = load_config(env=_env(global_root), cwd=tmp_path)
    assert config.default_timeout_s == 100.0  # from global
    assert config.max_depth == 7  # project wins


def test_env_overrides_win(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text("max_depth = 7\n", encoding="utf-8")
    env = _env(tmp_path / "empty")
    env["RUTHERFORD_MAX_DEPTH"] = "9"
    env["RUTHERFORD_DEFAULT_SAFETY"] = "propose"
    config = load_config(env=env, cwd=tmp_path)
    assert config.max_depth == 9
    assert config.default_safety_mode is SafetyMode.PROPOSE


def test_invalid_value_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text('default_safety_mode = "bogus"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)


def test_bad_toml_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text("this is not = = toml\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not parse"):
        load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)


def test_utf16_config_raises_config_error(tmp_path: Path) -> None:
    # Windows PowerShell 5.1 redirection writes UTF-16 by default; such a file must surface as
    # the structured ConfigError, not a raw UnicodeDecodeError.
    (tmp_path / "rutherford.toml").write_text("max_depth = 5\n", encoding="utf-16")
    with pytest.raises(ConfigError, match="could not parse"):
        load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)


def test_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing file"):
        load_config(env={"RUTHERFORD_CONFIG": str(tmp_path / "nope.toml")}, cwd=tmp_path)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text("not_a_real_key = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)


def test_deep_merge_nested() -> None:
    base = {"adapters": {"codex": {"default_model": "a"}}, "max_depth": 1}
    overlay = {"adapters": {"claude_code": {"default_model": "b"}}, "max_depth": 2}
    merged = deep_merge(base, overlay)
    assert merged["max_depth"] == 2
    assert merged["adapters"] == {
        "codex": {"default_model": "a"},
        "claude_code": {"default_model": "b"},
    }


def test_default_model_for() -> None:
    config = RutherfordConfig(adapters={"opencode": AdapterConfig(default_model="anthropic/claude-sonnet-4-6")})
    assert config.default_model_for("opencode") == "anthropic/claude-sonnet-4-6"
    assert config.default_model_for("absent") is None


def test_max_concurrency_defaults_to_max_targets(tmp_path: Path) -> None:
    config = load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)
    assert config.max_concurrency == config.max_targets == 8


def test_max_concurrency_follows_a_raised_max_targets(tmp_path: Path) -> None:
    # Raising max_targets must not silently throttle a single auto-panel to the old default of 8.
    (tmp_path / "rutherford.toml").write_text("max_targets = 16\n", encoding="utf-8")
    config = load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)
    assert config.max_targets == 16
    assert config.max_concurrency == 16


def test_explicit_max_concurrency_wins_over_the_derived_default(tmp_path: Path) -> None:
    (tmp_path / "rutherford.toml").write_text("max_targets = 16\nmax_concurrency = 4\n", encoding="utf-8")
    config = load_config(env=_env(tmp_path / "empty"), cwd=tmp_path)
    assert config.max_targets == 16
    assert config.max_concurrency == 4  # an explicit cap (e.g. a laptop) is respected


def test_probe_timeout_default_covers_the_longest_auth_probe() -> None:
    # The ceiling must not shorten a deliberate auth probe (codex doctor asks for 20s); an 8s default
    # truncated it and mis-reported a slow but valid auth as logged-out.
    assert RutherfordConfig().probe_timeout_s >= 20.0


def test_max_concurrency_env_override(tmp_path: Path) -> None:
    env = _env(tmp_path / "empty")
    env["RUTHERFORD_MAX_CONCURRENCY"] = "3"
    config = load_config(env=env, cwd=tmp_path)
    assert config.max_concurrency == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_depth": 0},
        {"default_timeout_s": -1.0},
        {"max_targets": 0},
        {"max_debate_rounds": 0},
        {"probe_timeout_s": 0},
        {"max_jobs": 0},
    ],
)
def test_out_of_range_numeric_fields_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RutherfordConfig(**kwargs)  # type: ignore[arg-type]


def test_dirs_resolve_to_absolute_and_a_missing_dir_warns_not_raises(tmp_path: Path) -> None:
    # An existing dir resolves to absolute; a missing one is kept (resolved) with a warning, not a
    # hard error -- an MCP stdio server should still start, and a missing trust path fails safe.
    missing = tmp_path / "nope"
    config = RutherfordConfig(trusted_workspaces=[str(tmp_path)], role_dirs=[str(missing)])
    assert config.trusted_workspaces == [str(tmp_path.resolve())]
    assert config.role_dirs == [str(missing.resolve())]
