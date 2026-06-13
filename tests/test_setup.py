# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for guided setup: plan building, applying, and the setup tool."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rutherford.config.loader import default_global_config_path, load_config
from rutherford.config.locations import home_dir
from rutherford.domain.enums import AuthState
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import AdapterCapabilities, AdapterStatus, AuthStatus, ProcessResult
from rutherford.io.serialize import decode
from rutherford.services.setup import apply_setup_plan, build_setup_plan, format_plan_summary
from rutherford.tools.setup import setup_tool
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app


def _status(
    adapter_id: str,
    *,
    installed: bool = True,
    auth: AuthState = AuthState.AUTHENTICATED,
    optional: bool = False,
) -> AdapterStatus:
    return AdapterStatus(
        id=adapter_id,
        display_name=adapter_id.title(),
        installed=installed,
        optional=optional,
        auth=AuthStatus(state=auth),
        capabilities=AdapterCapabilities(),
    )


def _env(home: Path) -> dict[str, str]:
    """An env that pins the config and panels roots under ``home`` on every platform."""
    config_root = str(home / "config-root")
    return {
        "APPDATA": config_root,  # Windows global config root
        "XDG_CONFIG_HOME": config_root,  # POSIX global config root
        "USERPROFILE": str(home),  # ~/.rutherford on Windows
        "HOME": str(home),  # ~/.rutherford on POSIX
    }


def _pin_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for key, value in _env(home).items():
        monkeypatch.setenv(key, value)


def test_plan_recommends_ready_clis_and_proposes_a_panel(tmp_path: Path) -> None:
    statuses = [
        _status("claude_code", auth=AuthState.AUTHENTICATED),
        _status("codex", auth=AuthState.UNKNOWN),  # unknown is optimistically ready
        _status("kiro", auth=AuthState.NEEDS_LOGIN),  # not ready
        _status("goose", installed=False),  # not ready
    ]
    plan = build_setup_plan(statuses, env=_env(tmp_path))
    assert plan.recommended_panel == ["claude_code", "codex"]
    panels_file = next(f for f in plan.files if f.kind == "panels")
    decoded = decode(panels_file.content)
    assert [t["cli"] for t in decoded["panels"]["default"]["targets"]] == ["claude_code", "codex"]


def test_optional_adapter_is_kept_out_of_the_starter_panel(tmp_path: Path) -> None:
    # An installed + authenticated optional adapter (a local model) is ready, but setup must not
    # auto-add it to the starter panel, and must not write any config for it -- it is opt-in.
    statuses = [_status("claude_code"), _status("codex"), _status("ollama", optional=True)]
    plan = build_setup_plan(statuses, env=_env(tmp_path))

    assert plan.recommended_panel == ["claude_code", "codex"]
    panels_file = next(f for f in plan.files if f.kind == "panels")
    assert [t["cli"] for t in decode(panels_file.content)["panels"]["default"]["targets"]] == ["claude_code", "codex"]
    config_file = next(f for f in plan.files if f.kind == "config")
    assert "[adapters.ollama]" not in config_file.content  # nothing forced on the user


def test_plan_skips_panel_when_fewer_than_two_ready(tmp_path: Path) -> None:
    plan = build_setup_plan([_status("claude_code")], env=_env(tmp_path))
    assert plan.recommended_panel == []
    assert not any(f.kind == "panels" for f in plan.files)
    assert any("Fewer than two" in note for note in plan.notes)


def test_config_file_carries_safety_mode_and_workspaces(tmp_path: Path) -> None:
    plan = build_setup_plan(
        [_status("a"), _status("b")],
        env=_env(tmp_path),
        safety_mode="propose",
        trusted_workspaces=[r"C:\work\repo"],
    )
    config = next(f for f in plan.files if f.kind == "config")
    assert "default_safety_mode = 'propose'" in config.content
    assert r"trusted_workspaces = ['C:\work\repo']" in config.content


def test_config_file_carries_default_persistence_when_chosen(tmp_path: Path) -> None:
    # 1-I: setup writes the first-run persistence choice into config, so the hint's "run setup" works.
    plan = build_setup_plan([_status("a"), _status("b")], env=_env(tmp_path), default_persistence="job")
    config = next(f for f in plan.files if f.kind == "config")
    assert "default_persistence = 'job'" in config.content


def test_default_persistence_is_omitted_when_not_chosen(tmp_path: Path) -> None:
    plan = build_setup_plan([_status("a"), _status("b")], env=_env(tmp_path))
    config = next(f for f in plan.files if f.kind == "config")
    assert "default_persistence" not in config.content  # not forced when the caller did not answer


def test_project_scope_writes_under_the_workspace_rutherford_dir(tmp_path: Path) -> None:
    # scope=project lands config + panels under <cwd>/.rutherford/, the same dir jobs use, so the
    # workspace-keyed first-run hint is answered for THIS workspace (decision 1-I).
    plan = build_setup_plan(
        [_status("a"), _status("b")],
        env=_env(tmp_path / "elsewhere"),  # global root points away, to prove project paths are used
        default_persistence="job",
        scope="project",
        cwd=tmp_path,
    )
    config = next(f for f in plan.files if f.kind == "config")
    assert config.path == str(tmp_path / ".rutherford" / "config.toml")
    assert "default_persistence = 'job'" in config.content
    panels = next(f for f in plan.files if f.kind == "panels")
    assert panels.path == str(tmp_path / ".rutherford" / "panels.toon")


def test_project_setup_then_load_applies_the_workspace_persistence_default(tmp_path: Path) -> None:
    # The full loop: setup (project scope) writes the default, and load_config reads it back for that
    # workspace -- the path the first-run hint actually directs the agent to.
    plan = build_setup_plan(
        [_status("a"), _status("b")],
        env=_env(tmp_path / "elsewhere"),
        default_persistence="job",
        scope="project",
        cwd=tmp_path,
    )
    apply_setup_plan(plan)
    assert (tmp_path / ".rutherford" / "config.toml").is_file()  # the .rutherford dir now exists too
    config = load_config(env=_env(tmp_path / "elsewhere"), cwd=tmp_path)
    assert config.default_persistence == "job"


def test_apply_writes_new_files_and_skips_existing(tmp_path: Path) -> None:
    plan = build_setup_plan([_status("a"), _status("b")], env=_env(tmp_path))
    written = apply_setup_plan(plan)
    assert len(written) == 2  # config + panels
    for path in written:
        assert Path(path).exists()

    # A second apply is a no-op (files now exist); --force overwrites.
    assert apply_setup_plan(plan) == []
    assert len(apply_setup_plan(plan, force=True)) == 2


def test_format_plan_summary_lists_clis_and_files(tmp_path: Path) -> None:
    plan = build_setup_plan([_status("a"), _status("b", auth=AuthState.NEEDS_LOGIN)], env=_env(tmp_path))
    summary = format_plan_summary(plan)
    assert "a: ready" in summary
    assert "b: installed" in summary
    assert "Files to write:" in summary


async def test_setup_tool_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_env(monkeypatch, tmp_path)
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await setup_tool(app)
    assert "applied: false" in out
    assert not default_global_config_path(os.environ).exists()  # nothing written


async def test_setup_tool_apply_writes_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_env(monkeypatch, tmp_path)
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await setup_tool(app, apply=True)
    assert "applied: true" in out
    assert default_global_config_path(os.environ).exists()
    assert (home_dir(os.environ) / ".rutherford" / "panels.toon").exists()


async def test_setup_tool_rejects_an_invalid_safety_mode_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The panel's MAJOR: setup used to persist the raw string into config.toml, where a bogus
    # value fails enum validation on the next load_config() and stops the server from starting.
    # It must be a clean INVALID_INPUT at the MCP boundary, with nothing written.
    _pin_env(monkeypatch, tmp_path)
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    with pytest.raises(RutherfordError, match="safety_mode"):
        await setup_tool(app, apply=True, safety_mode="bogus")
    assert not default_global_config_path(os.environ).exists()


async def test_setup_tool_rejects_an_invalid_default_persistence_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same boundary discipline as safety_mode: a bogus default_persistence is a clean INVALID_INPUT,
    # never written into config.toml where it would fail the Literal validation on the next load.
    _pin_env(monkeypatch, tmp_path)
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    with pytest.raises(RutherfordError, match="default_persistence"):
        await setup_tool(app, apply=True, default_persistence="sometimes")
    assert not default_global_config_path(os.environ).exists()


async def test_setup_tool_rejects_an_invalid_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_env(monkeypatch, tmp_path)
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    with pytest.raises(RutherfordError, match="scope"):
        await setup_tool(app, apply=True, scope="planet")
    assert not default_global_config_path(os.environ).exists()


def test_apply_does_not_clobber_a_file_created_after_planning(tmp_path: Path) -> None:
    # TOCTOU regression: the non-force write is exclusive-create, so a file that appears between
    # planning and applying (a concurrent setup, an editor save) is skipped, never overwritten.
    plan = build_setup_plan([_status("a"), _status("b")], env=_env(tmp_path))
    target = Path(plan.files[0].path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("operator-edited content", encoding="utf-8")
    written = apply_setup_plan(plan)
    assert plan.files[0].path not in written  # the racing file was skipped...
    assert target.read_text(encoding="utf-8") == "operator-edited content"  # ...and survives intact
