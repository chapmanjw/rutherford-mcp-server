# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the ``setup`` first-run helper: path resolution, the starter TOML, write/no-clobber, scopes."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

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


def _make_dir(parent: Path, name: str) -> Path:
    """Create ``parent/name`` from a sync helper (keeps the blocking mkdir out of an async body)."""
    target = parent / name
    target.mkdir(parents=True, exist_ok=True)
    return target


def _load_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file from a sync helper, for the same reason as :func:`_make_dir`."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


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


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids control characters in a path name")
async def test_trust_workspace_survives_a_control_char_in_cwd(tmp_path, monkeypatch) -> None:
    """A cwd holding a control character must still scaffold loadable TOML.

    On Linux and macOS every byte except ``/`` and NUL is a legal filename byte, so a tab or newline
    can reach the quoter. Emitting it raw would write a config ``tomllib`` then rejects on every later
    load -- and because setup never clobbers, it could not repair the file it had just written.
    """
    workspace = _make_dir(tmp_path, "ws\nwith\tcontrols")
    monkeypatch.chdir(workspace)
    await setup_tool(_app(), scope="project", write=True, trust_workspace=True)
    parsed = _load_toml(workspace / ".rutherford" / "config.toml")
    assert _resolved(parsed["trusted_workspaces"][0]) == _resolved(str(workspace))


async def test_a_non_utf8_cwd_is_refused_before_anything_is_written(tmp_path, monkeypatch) -> None:
    """A cwd that cannot be encoded must fail with NO file and NO directory left behind.

    ``Path.write_text`` truncates its target before it encodes, so letting the bad character reach the
    write would leave a zero-byte ``config.toml`` -- and the never-clobber guard would then refuse to
    regenerate it, making the breakage permanent. The refusal has to happen before the open.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: Path(f"{tmp_path}\udcff")))
    with pytest.raises(RutherfordError) as exc:
        await setup_tool(_app(), scope="project", write=True, trust_workspace=True)
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert not (tmp_path / ".rutherford").exists()  # nothing was created before the refusal


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


async def test_bedrock_env_scaffolds_a_commented_claude_env_block(tmp_path, monkeypatch) -> None:
    # On a Bedrock/Vertex host, setup surfaces the per-agent env fix as a commented block + a flag.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    data = decode(await setup_tool(_app(), scope="project"))
    assert data["bedrock_detected"] is True
    assert "bedrock_note" in data
    assert "[agents.claude_code.env]" in data["content"]
    assert "ANTHROPIC_CUSTOM_MODEL_OPTION" in data["content"]
    # The block is all comments, so the scaffold still parses and validates (extra="forbid").
    RutherfordConfig.model_validate(tomllib.loads(data["content"]))


async def test_no_bedrock_block_off_bedrock(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    data = decode(await setup_tool(_app(), scope="project"))
    assert "bedrock_detected" not in data
    assert "[agents.claude_code.env]" not in data["content"]


async def test_starter_reflects_effective_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = RutherfordConfig(
        default_timeout_s=120.0,
        max_targets=5,
        auto_detect_local_models=False,
        default_persistence="job",
        synthesize_default=True,
    )
    data = decode(await setup_tool(_app(config), scope="project"))
    parsed = tomllib.loads(data["content"])
    assert parsed["default_timeout_s"] == 120
    assert parsed["max_targets"] == 5
    assert parsed["auto_detect_local_models"] is False
    # The F2 durability + synthesis knobs are scaffolded at their effective defaults, and the panels.toon
    # pointer is present so a reader knows where named panels live (v2 setup parity).
    assert parsed["default_persistence"] == "job"
    assert parsed["synthesize_default"] is True
    assert "panels.toon" in data["content"]
