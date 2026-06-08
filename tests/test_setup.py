# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for guided setup: plan building, applying, and the setup tool."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rutherford.config.loader import default_global_config_path
from rutherford.config.locations import home_dir
from rutherford.domain.enums import AuthState
from rutherford.domain.models import AdapterCapabilities, AdapterStatus, AuthStatus, ProcessResult
from rutherford.io.serialize import decode
from rutherford.services.setup import apply_setup_plan, build_setup_plan, format_plan_summary
from rutherford.tools.setup import setup_tool
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app


def _status(adapter_id: str, *, installed: bool = True, auth: AuthState = AuthState.AUTHENTICATED) -> AdapterStatus:
    return AdapterStatus(
        id=adapter_id,
        display_name=adapter_id.title(),
        installed=installed,
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


def test_plan_auto_configures_ollama_default_and_keeps_it_out_of_the_panel(tmp_path: Path) -> None:
    ollama = AdapterStatus(
        id="ollama",
        display_name="Ollama (local model)",
        installed=True,
        optional=True,
        auth=AuthStatus(state=AuthState.AUTHENTICATED),
        models=["llama3", "qwen2.5-coder:latest", "phi3"],
        capabilities=AdapterCapabilities(),
    )
    plan = build_setup_plan([_status("claude_code"), _status("codex"), ollama], env=_env(tmp_path))

    config_file = next(f for f in plan.files if f.kind == "config")
    # Writes a per-adapter section, preferring the coding model from what is installed.
    assert "[adapters.ollama]" in config_file.content
    assert "default_model = 'qwen2.5-coder:latest'" in config_file.content
    assert any("Ollama default model" in note for note in plan.notes)
    # The optional local model is configured but NOT auto-added to the starter panel.
    assert plan.recommended_panel == ["claude_code", "codex"]


def test_setup_uses_the_chosen_ollama_model_over_the_suggestion(tmp_path: Path) -> None:
    ollama = AdapterStatus(
        id="ollama",
        display_name="Ollama (local model)",
        installed=True,
        optional=True,
        auth=AuthStatus(state=AuthState.AUTHENTICATED),
        models=["qwen2.5-coder:latest", "phi3"],
        capabilities=AdapterCapabilities(),
    )
    # The wizard's pick wins over the suggested coding model.
    plan = build_setup_plan([_status("claude_code"), ollama], env=_env(tmp_path), ollama_model="phi3")
    config_file = next(f for f in plan.files if f.kind == "config")
    assert "default_model = 'phi3'" in config_file.content
    # The note tells the user, in plain terms, how to change it.
    note = next(n for n in plan.notes if "Ollama default model" in n)
    assert "ollama list" in note and "init --force" in note


def test_setup_skip_leaves_ollama_unconfigured_but_notes_how(tmp_path: Path) -> None:
    ollama = AdapterStatus(
        id="ollama",
        display_name="Ollama (local model)",
        installed=True,
        optional=True,
        auth=AuthStatus(state=AuthState.AUTHENTICATED),
        models=["coder-next:latest"],
        capabilities=AdapterCapabilities(),
    )
    plan = build_setup_plan([_status("claude_code"), ollama], env=_env(tmp_path), configure_ollama=False)
    config_file = next(f for f in plan.files if f.kind == "config")
    assert "[adapters.ollama]" not in config_file.content  # nothing forced on the user
    assert any("no default model was set" in note for note in plan.notes)


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
