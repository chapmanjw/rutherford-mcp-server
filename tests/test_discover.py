# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for detect-only discovery, the discover tool (propose-to-config), and the CLI / server wrapper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rutherford import server
from rutherford.acp.conformance import ConformanceReport
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.discovery import discover_agents, resolve_local_command
from rutherford.acp.registry import RegistryAgent
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import decode
from rutherford.tools.discover import discover_tool

# A registry fixture: two brand-new agents (one will probe ok, one error) and a codex alias. One platform
# key per binary; the parser's `_any_platform` fallback makes the fixture cross-platform.
_FIXTURE = {
    "version": "1.0.0",
    "agents": [
        {
            "id": "newgood",
            "name": "New Good",
            "distribution": {"binary": {"linux-x86_64": {"cmd": "./newgood", "args": ["acp"]}}},
        },
        {
            "id": "newbad",
            "name": "New Bad",
            "distribution": {"binary": {"linux-x86_64": {"cmd": "./newbad", "args": ["acp"]}}},
        },
        {"id": "codex-acp", "name": "Codex", "distribution": {"npx": {"package": "@zed/codex-acp@1.0.0", "args": []}}},
    ],
}


def _install(home: Path, name: str) -> Path:
    """Create a dummy executable under a ~/.<vendor>/bin/ tree so detection's dir-scan resolves it."""
    target = home / ".tools" / "bin" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    return target


def _fixture_url(tmp_path: Path, data: object = _FIXTURE) -> str:
    target = tmp_path / "registry.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target.as_uri()


def _agent(agent_id: str, *, bin_name: str, args: tuple[str, ...] = ()) -> RegistryAgent:
    return RegistryAgent(id=agent_id, name=agent_id, description="", candidates=((bin_name, args),))


# --- resolution (filesystem) ------------------------------------------------------------------------------


def test_resolve_finds_a_nested_install_dir(tmp_path: Path) -> None:
    _install(tmp_path, "qodercli")  # ~/.tools/bin/qodercli
    agent = _agent("qoder", bin_name="qodercli", args=("--acp",))
    resolved = resolve_local_command(agent, env={"PATH": ""}, home=tmp_path)
    assert resolved is not None
    command, found_at = resolved
    assert command[0].endswith("qodercli") and command[1:] == ("--acp",)
    assert "qodercli" in found_at


def test_resolve_handles_a_subdir_one_level_deep(tmp_path: Path) -> None:
    nested = tmp_path / ".tools" / "bin" / "qodercli" / "qodercli.exe"
    nested.parent.mkdir(parents=True)
    nested.write_text("x", encoding="utf-8")
    resolved = resolve_local_command(_agent("qoder", bin_name="qodercli"), env={"PATH": ""}, home=tmp_path)
    assert resolved is not None and resolved[1].endswith("qodercli.exe")


def test_resolve_returns_none_when_absent(tmp_path: Path) -> None:
    assert resolve_local_command(_agent("ghost", bin_name="ghost"), env={"PATH": ""}, home=tmp_path) is None


def test_resolve_refuses_an_interpreter_bin(tmp_path: Path) -> None:
    # A hostile registry naming a shell/interpreter (with hostile args) must never resolve, even if present.
    _install(tmp_path, "powershell")
    evil = RegistryAgent(id="evil", name="Evil", description="", candidates=(("powershell", ("-c", "calc")),))
    assert resolve_local_command(evil, env={"PATH": ""}, home=tmp_path) is None


def test_resolve_skips_an_interpreter_candidate_but_takes_a_real_one(tmp_path: Path) -> None:
    _install(tmp_path, "realbin")
    mixed = RegistryAgent(
        id="mixed", name="Mixed", description="", candidates=(("node", ("x",)), ("realbin", ("acp",)))
    )
    resolved = resolve_local_command(mixed, env={"PATH": ""}, home=tmp_path)
    assert resolved is not None and resolved[0][0].endswith("realbin") and resolved[0][1:] == ("acp",)


@pytest.mark.parametrize(
    "interp",
    [
        "python3.12",
        "pythonw",
        "pyw",
        "rubyw",
        "python3.11m",
        "perl5.38",
        "node20",
        "PowerShell",
        "pwsh",
        "uvx",
        "node.cmd",
        "npx.cmd",
        "powershell.exe",
        "python3.12.EXE",
        "bash",
        # the family classifier must also catch debug/preview suffixes and single-letter interpreters
        "python3-dbg",
        "python3.11-dbg",
        "pwsh-preview",
        "R",
        "Rscript",
        "Rterm",
        "Rgui.exe",
        "deno",
        "bunx",
        "gdb",
        "jq",
    ],
)
def test_resolve_refuses_interpreters_and_shims(interp: str, tmp_path: Path) -> None:
    # Versioned, debug/preview, variant, single-letter, and .cmd/.exe-shimmed interpreters must all be refused.
    _install(tmp_path, interp)
    evil = RegistryAgent(id="evil", name="Evil", description="", candidates=((interp, ("-e", "x")),))
    assert resolve_local_command(evil, env={"PATH": ""}, home=tmp_path) is None


@pytest.mark.parametrize(
    "legit",
    [
        "goose",
        "qodercli",
        "gemini",
        "gemini.cmd",
        "cline.CMD",
        "qwen.CMD",
        "share-cli",
        "shellfish",
        "nodecli",
        "draw",
        "pi-acp",
        "pi",
        "nushell",
        "rover",
        "phpstorm-agent",
    ],
)
def test_resolve_keeps_a_legitimate_agent_bin(legit: str, tmp_path: Path) -> None:
    # The guard must not false-positive on a real agent name (incl. a .cmd shim with a non-interpreter family).
    _install(tmp_path, legit)
    agent = RegistryAgent(id="ag", name="ag", description="", candidates=((legit, ("acp",)),))
    assert resolve_local_command(agent, env={"PATH": ""}, home=tmp_path) is not None


# --- discover_agents (with a stubbed probe) ---------------------------------------------------------------


async def _fake_probe(
    descriptor: AgentDescriptor, *, cwd: str | None = None, timeout_s: float = 60.0
) -> ConformanceReport:
    ok = "good" in descriptor.id or descriptor.id == "codex-acp"
    return ConformanceReport(
        agent_id=descriptor.id,
        status="ok" if ok else "error",
        installed=True,
        answered=ok,
        detail="probed",
        duration_s=0.01,
    )


async def test_discover_agents_probes_and_flags_roster(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("rutherford.acp.discovery.probe_agent", _fake_probe)
    for name in ("newgood", "newbad", "codex-acp"):
        _install(tmp_path, name)
    agents = [
        _agent("newgood", bin_name="newgood", args=("acp",)),
        _agent("newbad", bin_name="newbad", args=("acp",)),
        _agent("codex-acp", bin_name="codex-acp"),
    ]
    found = await discover_agents(agents, known_ids={"codex"}, env={"PATH": ""}, home=tmp_path)
    by_id = {d.id: d for d in found}
    assert [d.id for d in found] == ["codex-acp", "newbad", "newgood"]  # sorted
    assert by_id["newgood"].status == "ok" and by_id["newbad"].status == "error"
    # codex-acp aliases to the built-in `codex`, so it is recognized as already-in-roster (not a new seat).
    assert by_id["codex-acp"].already_in_roster is True
    assert by_id["newgood"].already_in_roster is False


async def test_discover_agents_no_probe_skips_spawning(tmp_path: Path, monkeypatch: Any) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("probe_agent must not be called when probe=False")

    monkeypatch.setattr("rutherford.acp.discovery.probe_agent", _boom)
    _install(tmp_path, "newgood")
    found = await discover_agents(
        [_agent("newgood", bin_name="newgood")], known_ids=set(), probe=False, env={"PATH": ""}, home=tmp_path
    )
    assert len(found) == 1 and found[0].status is None


# --- the discover tool ------------------------------------------------------------------------------------


def _app(*builtin_ids: str) -> AppContext:
    descriptors = DescriptorRegistry(
        [AgentDescriptor(i, i, (f"{i}-acp",)) for i in builtin_ids]
        or [AgentDescriptor("codex", "Codex", ("codex-acp",))]
    )
    return build_app_context(config=RutherfordConfig(), descriptors=descriptors)


async def test_discover_tool_proposes_new_drivers(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("newgood", "newbad", "codex-acp"):
        _install(home, name)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path))
    out = await discover_tool(_app("codex"), probe=False)  # probe off -> all new found agents proposed
    data = decode(out)
    assert data["registry_source"] == "network" and data["registry_agents"] == 3
    assert set(data["new_drivers"]) == {"newgood", "newbad"}  # codex-acp aliases to the built-in codex
    assert "[agents.newgood]" in out and "[agents.codex-acp]" not in out


async def test_discover_tool_filters_to_drivers_when_probing(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for name in ("newgood", "newbad", "codex-acp"):
        _install(home, name)
    monkeypatch.setattr("rutherford.acp.discovery.probe_agent", _fake_probe)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path))
    out = await discover_tool(_app("codex"), probe=True)
    # newbad probes error, so only the driving newgood is proposed.
    assert decode(out)["new_drivers"] == ["newgood"]


async def test_discover_tool_write_appends_then_skips_existing(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    _install(home, "newgood")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, {"version": "1", "agents": [_FIXTURE["agents"][0]]})
    )
    monkeypatch.chdir(proj)
    out = await discover_tool(_app("codex"), probe=False, write=True, scope="project")
    config = proj / ".rutherford" / "config.toml"
    assert config.exists() and "[agents.newgood]" in config.read_text(encoding="utf-8")
    assert decode(out)["written_ids"] == ["newgood"]
    # A second write must not duplicate the section.
    out2 = await discover_tool(_app("codex"), probe=False, write=True, scope="project")
    body = config.read_text(encoding="utf-8")
    assert body.count("[agents.newgood]") == 1
    assert decode(out2)["skipped_existing"] == ["newgood"]


async def test_discover_tool_rejects_a_bad_scope() -> None:
    with pytest.raises(RutherfordError):
        await discover_tool(_app("codex"), write=True, scope="nonsense")


async def test_server_discover_wrapper(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _install(home, "newgood")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, {"version": "1", "agents": [_FIXTURE["agents"][0]]})
    )
    monkeypatch.setattr(server, "_APP", _app("codex"))
    out = await server.discover(probe=False)
    assert "newgood" in out


def test_server_discover_cli(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _install(home, "newgood")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, {"version": "1", "agents": [_FIXTURE["agents"][0]]})
    )
    server._discover(["--no-probe"])
    out = capsys.readouterr().out
    assert "fetching the ACP registry" in out and "newgood" in out


# --- hardening: unsafe ids, malformed config, dedupe, TOML injection ---------------------------------------


def _binary_entry(agent_id: str, bin_name: str, name: str = "") -> dict[str, Any]:
    return {
        "id": agent_id,
        "name": name or agent_id,
        "distribution": {"binary": {"linux-x86_64": {"cmd": f"./{bin_name}", "args": ["acp"]}}},
    }


async def test_discover_tool_skips_an_unsafe_id(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _install(home, "badbin")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    fixture = {"version": "1", "agents": [_binary_entry("bad.id]", "badbin")]}
    monkeypatch.setenv("RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, fixture))
    data = decode(await discover_tool(_app("codex"), probe=False))
    assert data["new_drivers"] == [] and data["skipped_unsafe_ids"] == ["bad.id]"]
    assert "proposed_config" not in data  # an unsafe id is never rendered into config


async def test_discover_tool_refuses_to_write_into_a_malformed_config(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    _install(home, "newgood")
    broken = proj / ".rutherford" / "config.toml"
    broken.parent.mkdir(parents=True)
    broken.write_text("this is = = not valid toml", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, {"version": "1", "agents": [_FIXTURE["agents"][0]]})
    )
    monkeypatch.chdir(proj)
    data = decode(await discover_tool(_app("codex"), probe=False, write=True, scope="project"))
    assert data["written"] is False and "not valid TOML" in data["note"]
    assert broken.read_text(encoding="utf-8") == "this is = = not valid toml"  # untouched


async def test_discover_tool_dedupes_a_repeated_registry_id(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _install(home, "dupbin")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    fixture = {"version": "1", "agents": [_binary_entry("dup", "dupbin"), _binary_entry("dup", "dupbin")]}
    monkeypatch.setenv("RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, fixture))
    out = await discover_tool(_app("codex"), probe=False)
    assert decode(out)["new_drivers"] == ["dup"]  # one entry, not two
    assert out.count("[agents.dup]") == 1


async def test_discover_tool_write_neutralizes_a_toml_injection_in_the_name(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    _install(home, "newgood")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    evil_name = "Evil\n[agents.injected]\nfoo = 1"
    fixture = {"version": "1", "agents": [_binary_entry("safeid", "newgood", name=evil_name)]}
    monkeypatch.setenv("RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, fixture))
    monkeypatch.chdir(proj)
    await discover_tool(_app("codex"), probe=False, write=True, scope="project")
    import tomllib

    parsed = tomllib.loads((proj / ".rutherford" / "config.toml").read_text(encoding="utf-8"))
    # The newline in the name was collapsed, so no injected table was smuggled in -- valid TOML, only safeid.
    assert "injected" not in parsed.get("agents", {})
    assert "safeid" in parsed["agents"]


async def test_discover_tool_write_escapes_a_control_char_in_an_arg(tmp_path: Path, monkeypatch: Any) -> None:
    import tomllib

    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    _install(home, "newgood")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    # A registry arg carrying a newline must be escaped, not written raw (which would corrupt the TOML).
    entry = {
        "id": "argy",
        "name": "Argy",
        "distribution": {"binary": {"linux-x86_64": {"cmd": "./newgood", "args": ["--flag\nevil = 1"]}}},
    }
    monkeypatch.setenv("RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, {"version": "1", "agents": [entry]}))
    monkeypatch.chdir(proj)
    await discover_tool(_app("codex"), probe=False, write=True, scope="project")
    parsed = tomllib.loads((proj / ".rutherford" / "config.toml").read_text(encoding="utf-8"))  # must parse
    assert "evil" not in parsed.get("agents", {})  # no smuggled key
    assert parsed["agents"]["argy"]["command"][-1] == "--flag\nevil = 1"  # the newline survives, escaped


async def test_discover_tool_writes_to_the_config_the_loader_reads(tmp_path: Path, monkeypatch: Any) -> None:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    _install(home, "newgood")
    # The loader reads the first existing of (rutherford.toml, .rutherford.toml, .rutherford/config.toml) and
    # stops, so a write must land in rutherford.toml here -- not a fresh .rutherford/config.toml it would ignore.
    (proj / "rutherford.toml").write_text('default_safety_mode = "read_only"\n', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "RUTHERFORD_ACP_REGISTRY_URL", _fixture_url(tmp_path, {"version": "1", "agents": [_FIXTURE["agents"][0]]})
    )
    monkeypatch.chdir(proj)
    data = decode(await discover_tool(_app("codex"), probe=False, write=True, scope="project"))
    assert data["write_path"].endswith("rutherford.toml") and not data["write_path"].endswith("config.toml")
    assert "[agents.newgood]" in (proj / "rutherford.toml").read_text(encoding="utf-8")
    assert not (proj / ".rutherford" / "config.toml").exists()  # never wrote the ignored file
