# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the thin layers: common parsers, context helpers, the tools, and the FastMCP server."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from rutherford import server
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context, error_payload_from, tool_error, tool_success
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import decode
from rutherford.tools.capabilities import capabilities_tool
from rutherford.tools.common import ensure_known_agent, parse_safety_mode, resolve_safety_mode
from rutherford.tools.delegate import delegate_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_SCRIPT = str(Path(__file__).resolve().parent / "fake_acp_agent.py")
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, FAKE_SCRIPT))
FAKE2 = AgentDescriptor("fake2", "Fake Two", (sys.executable, FAKE_SCRIPT))


def _app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE]))


def _panel_app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE, FAKE2]))


def test_common_parsers() -> None:
    assert parse_safety_mode("write") is SafetyMode.WRITE
    assert parse_safety_mode(SafetyMode.YOLO) is SafetyMode.YOLO
    assert resolve_safety_mode(None, SafetyMode.READ_ONLY) is SafetyMode.READ_ONLY
    assert resolve_safety_mode("yolo", SafetyMode.READ_ONLY) is SafetyMode.YOLO
    with pytest.raises(RutherfordError):
        parse_safety_mode("bogus")
    registry = DescriptorRegistry([FAKE])
    ensure_known_agent(registry, "fake")
    with pytest.raises(RutherfordError):
        ensure_known_agent(registry, "nope")


def test_envelope_helpers() -> None:
    assert "1" in tool_success({"a": 1})
    error = decode(tool_error(ErrorCode.INTERNAL, "boom", {"k": "v"}))
    assert error["error"]["code"] == "INTERNAL" and error["error"]["details"]["k"] == "v"
    assert "INVALID_INPUT" in error_payload_from(RutherfordError(ErrorCode.INVALID_INPUT, "bad"))


async def test_capabilities_tool_lists_agents() -> None:
    data = decode(await capabilities_tool(_app()))
    assert any(agent["id"] == "fake" for agent in data["agents"])


async def test_delegate_tool_ok_and_unknown() -> None:
    out = await delegate_tool(_app(), cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    assert "42" in out
    with pytest.raises(RutherfordError):
        await delegate_tool(_app(), cli="nope", prompt="x")


async def test_server_guarded_paths() -> None:
    async def ok() -> str:
        return "fine"

    assert await server._guarded(ok()) == "fine"

    async def rutherford_error() -> str:
        raise RutherfordError(ErrorCode.INVALID_INPUT, "no")

    with pytest.raises(ToolError):
        await server._guarded(rutherford_error())

    async def crash() -> str:
        raise ValueError("boom")

    with pytest.raises(ToolError):
        await server._guarded(crash())


async def test_server_tool_wrappers(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", _app())
    out = await server.delegate(cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    assert "42" in out
    caps = await server.capabilities()
    assert "fake" in caps


def test_get_app_and_main(monkeypatch: Any) -> None:
    monkeypatch.setattr(server, "_APP", None)
    app = server.get_app()
    assert app is not None
    assert server.get_app() is app  # cached on the second call
    monkeypatch.setattr(server, "_APP", None)
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: None)
    server.main()
    assert server._APP is not None


# --- Advisory persistence notices (F2 nudge) --------------------------------------------------------------


def _write_config(tmp_path: Path) -> None:
    """Create a project ``config.toml`` so the first-run hint is suppressed (its existence is all that matters).

    A sync helper: the first-run hint keys off the config FILE, not the ``.rutherford`` dir (a persisted run's
    ledger creates the dir but never the file). Callable from an async test without tripping ASYNC240.
    """
    cfg = tmp_path / ".rutherford" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('default_safety_mode = "read_only"\n', encoding="utf-8")


def test_persistence_notice_first_run_hint_emits_once(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)  # a clean workspace with no .rutherford/
    app = _app()
    first = app.persistence_notice(persisted=False, complex_run=False, external_tracking=False)
    assert first is not None and "ephemeral" in first  # the one-time first-run setup hint
    assert app.setup_hint_emitted is True
    # The hint is one-time per session: a second simple call in the same workspace stays quiet.
    assert app.persistence_notice(persisted=False, complex_run=False, external_tracking=False) is None


def test_persistence_notice_first_run_hint_survives_a_persisted_first_call(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".rutherford" / "jobs").mkdir(parents=True)  # a ledger side effect, NOT a user config
    app = _app()
    # The hint keys off config.toml, so a persisted run's jobs dir does not wrongly suppress it.
    first = app.persistence_notice(persisted=True, complex_run=True, external_tracking=False)
    assert first is not None and "ephemeral" in first


@pytest.mark.parametrize("name", ["rutherford.toml", ".rutherford.toml", ".rutherford/config.toml"])
def test_persistence_notice_recognizes_every_project_config_name(name: str, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / name
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('default_safety_mode = "read_only"\n', encoding="utf-8")
    app = _app()
    # A workspace configured under ANY recognized project name must not get the first-run "no config" hint.
    assert app.persistence_notice(persisted=False, complex_run=False, external_tracking=False) is None


def test_persistence_notice_suggests_keeping_a_complex_run(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)  # a project config exists, so the first-run hint is suppressed
    app = _app()
    notice = app.persistence_notice(persisted=False, complex_run=True, external_tracking=False)
    assert notice is not None and "persist=true" in notice
    assert "ephemeral" not in notice  # only the suggest-a-job line, not the first-run hint


def test_persistence_notice_external_tracking_suppresses_suggestion(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    app = _app()
    # An orchestrator that tracks the run itself silences the suggest-a-job nudge.
    assert app.persistence_notice(persisted=False, complex_run=True, external_tracking=True) is None


def test_persistence_notice_quiet_for_a_persisted_simple_run(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    app = _app()
    # Already persisted, and a simple (single read-only) run -- nothing to advise.
    assert app.persistence_notice(persisted=True, complex_run=True, external_tracking=False) is None
    assert app.persistence_notice(persisted=False, complex_run=False, external_tracking=False) is None


async def test_consensus_tool_wires_the_notice(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)  # no .rutherford/ -> the first-run hint rides the result
    from rutherford.tools.consensus import consensus_tool

    # Assert against the raw TOON text: a ConsensusResult's non-uniform ``voices`` list-array is not
    # round-trippable through python-toon's decoder (a known decoder limitation, independent of the notice).
    out = await consensus_tool(
        _panel_app(), prompt="what is 17 + 25?", targets=["fake", "fake2"], working_dir=str(REPO_ROOT)
    )
    assert "notice:" in out and "ephemeral" in out  # a multi-voice panel in a config-less workspace nudges


async def test_consensus_external_tracking_silences_the_suggestion(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)  # project config present -> no first-run hint either
    from rutherford.tools.consensus import consensus_tool

    out = await consensus_tool(
        _panel_app(),
        prompt="what is 17 + 25?",
        targets=["fake", "fake2"],
        working_dir=str(REPO_ROOT),
        external_tracking=True,
    )
    assert "notice:" not in out  # both nudges suppressed -> the field is absent from the wire


# --- The `init` first-run CLI -----------------------------------------------------------------------------


def test_init_writes_a_project_config(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.chdir(tmp_path)
    server._init(["--yes"])  # --yes skips the confirmation prompt
    target = tmp_path / ".rutherford" / "config.toml"
    assert target.exists()
    out = capsys.readouterr().out
    assert "built-in agent(s)" in out and "doctor" in out


def test_init_does_not_clobber_an_existing_config(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".rutherford" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text("max_targets = 9\n", encoding="utf-8")
    server._init(["--yes"])
    assert target.read_text(encoding="utf-8") == "max_targets = 9\n"  # untouched
    assert "already exists" in capsys.readouterr().out


def test_init_aborts_on_a_declined_prompt(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # decline the confirmation
    server._init([])
    assert not (tmp_path / ".rutherford" / "config.toml").exists()
    assert "aborted" in capsys.readouterr().out


def test_init_global_scope_targets_the_global_path(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    # Redirect the global config dir to tmp_path on every platform so nothing touches the real home.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    server._init(["--global", "--yes"])
    assert "config target (global)" in capsys.readouterr().out


def test_init_global_is_not_blocked_by_a_broken_project_config(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    # A malformed PROJECT config must not block a GLOBAL init (a different scope): init is the bootstrap
    # command, so it scaffolds from defaults and warns rather than exiting.
    monkeypatch.chdir(tmp_path)
    global_dir = tmp_path / "globalcfg"
    monkeypatch.setenv("APPDATA", str(global_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_dir))
    broken = tmp_path / ".rutherford" / "config.toml"
    broken.parent.mkdir(parents=True)
    broken.write_text('default_safety_mode = "nonsense"\n', encoding="utf-8")
    server._init(["--global", "--yes"])  # must NOT raise SystemExit
    captured = capsys.readouterr()
    assert "scaffolding from defaults" in captured.err  # the warning, on stderr
    assert "wrote" in captured.out  # it still scaffolded the global config
