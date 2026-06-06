# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the panels loader, store, cache, and the panel= tool paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rutherford.config.panels import Panel, PanelCache, PanelStore, PanelTarget, load_panels
from rutherford.domain.enums import Stance
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ProcessResult
from rutherford.io.serialize import encode
from rutherford.tools.consensus import consensus_tool
from rutherford.tools.debate import debate_tool
from rutherford.tools.panels import reload_panels_tool
from rutherford.tools.review import review_tool
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app

KNOWN = ["claude_code", "codex", "kiro"]


def _problems(error: RutherfordError) -> list[dict[str, Any]]:
    """Pull the structured problem list off a ``PANEL_INVALID`` error."""
    assert error.details is not None
    problems = error.details["problems"]
    assert isinstance(problems, list)
    return problems


def _write_panels(directory: Path, panels: dict[str, Any]) -> None:
    """Write a ``panels.toon`` file built from ``panels`` (name -> record)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "panels.toon").write_text(encode({"panels": panels}), encoding="utf-8")


def _env(home: Path, config_dir: Path | None = None) -> dict[str, str]:
    env = {"USERPROFILE": str(home), "HOME": str(home)}
    if config_dir is not None:
        env["RUTHERFORD_CONFIG_DIR"] = str(config_dir)
    return env


# --- loader: discovery, precedence, merge -------------------------------------------------------


def test_loads_a_panel_from_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(
        home / ".rutherford",
        {"duo": {"description": "two", "targets": [{"cli": "claude_code", "model": "opus"}, {"cli": "codex"}]}},
    )
    store = load_panels(KNOWN, env=_env(home), cwd=tmp_path / "proj")
    panel = store.get("duo")
    assert [target.cli for target in panel.targets] == ["claude_code", "codex"]
    assert panel.to_targets()[0].model == "opus"
    assert panel.strategy == "all-voices"  # default when unset


def test_project_overrides_home_by_name(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    _write_panels(
        home / ".rutherford",
        {
            "duo": {"targets": [{"cli": "claude_code"}, {"cli": "codex"}]},
            "solo_home": {"targets": [{"cli": "kiro"}]},
        },
    )
    _write_panels(
        proj / ".rutherford",
        {
            "duo": {"targets": [{"cli": "kiro"}, {"cli": "codex"}]},  # same name: project wins
            "solo_proj": {"targets": [{"cli": "codex"}]},
        },
    )
    store = load_panels(KNOWN, env=_env(home), cwd=proj)
    assert set(store.names()) == {"duo", "solo_home", "solo_proj"}  # union of names
    assert [target.cli for target in store.get("duo").targets] == ["kiro", "codex"]  # closest scope


def test_config_dir_overrides_project(tmp_path: Path) -> None:
    home, proj, cfg = tmp_path / "home", tmp_path / "proj", tmp_path / "cfg"
    _write_panels(proj / ".rutherford", {"duo": {"targets": [{"cli": "kiro"}, {"cli": "codex"}]}})
    _write_panels(cfg, {"duo": {"targets": [{"cli": "claude_code"}, {"cli": "codex"}]}})
    store = load_panels(KNOWN, env=_env(home, cfg), cwd=proj)
    assert [target.cli for target in store.get("duo").targets] == ["claude_code", "codex"]  # env wins


def test_no_files_is_an_empty_store(tmp_path: Path) -> None:
    store = load_panels(KNOWN, env=_env(tmp_path / "nohome"), cwd=tmp_path / "noproj")
    assert store.names() == []


# --- loader: validation -------------------------------------------------------------------------


def test_unknown_panel_name_raises_with_available(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home / ".rutherford", {"duo": {"targets": [{"cli": "codex"}, {"cli": "kiro"}]}})
    store = load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p")
    with pytest.raises(RutherfordError) as info:
        store.get("missing")
    assert info.value.code == "PANEL_NOT_FOUND"
    assert "duo" in info.value.message


def test_unknown_cli_is_a_validation_error_with_target_index(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home / ".rutherford", {"bad": {"targets": [{"cli": "codex"}, {"cli": "nope"}]}})
    with pytest.raises(RutherfordError) as info:
        load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p")
    assert info.value.code == "PANEL_INVALID"
    problems = _problems(info.value)
    assert any(problem.get("target") == 1 and "nope" in problem["error"] for problem in problems)


def test_every_problem_surfaces_in_one_pass(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(
        home / ".rutherford",
        {"bad": {"junk": 1, "targets": [{"cli": "nope1"}, {"cli": "nope2", "stance": "sideways"}]}},
    )
    with pytest.raises(RutherfordError) as info:
        load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p")
    problems = _problems(info.value)
    # unknown panel key + two unknown clis + one bad stance = at least four, not just the first.
    assert len(problems) >= 4


def test_missing_panels_table_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "home" / ".rutherford"
    directory.mkdir(parents=True)
    (directory / "panels.toon").write_text(encode({"notpanels": {"x": 1}}), encoding="utf-8")
    with pytest.raises(RutherfordError) as info:
        load_panels(KNOWN, env=_env(tmp_path / "home"), cwd=tmp_path / "p")
    assert info.value.code == "PANEL_INVALID"
    assert "top-level 'panels'" in info.value.message


def test_malformed_toon_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "home" / ".rutherford"
    directory.mkdir(parents=True)
    (directory / "panels.toon").write_text("items[3]: 1,2\n", encoding="utf-8")  # invalid TOON
    with pytest.raises(RutherfordError) as info:
        load_panels(KNOWN, env=_env(tmp_path / "home"), cwd=tmp_path / "p")
    assert info.value.code == "PANEL_INVALID"
    assert "could not read panels file" in _problems(info.value)[0]["error"]


def test_empty_targets_list_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home / ".rutherford", {"empty": {"targets": []}})
    with pytest.raises(RutherfordError) as info:
        load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p")
    assert any("non-empty 'targets'" in problem["error"] for problem in _problems(info.value))


def test_unknown_strategy_in_a_panel_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(
        home / ".rutherford",
        {"bad": {"strategy": "plurality", "targets": [{"cli": "codex"}, {"cli": "kiro"}]}},
    )
    with pytest.raises(RutherfordError) as info:
        load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p")
    assert any("unknown strategy" in problem["error"] for problem in _problems(info.value))


# --- cache: lazy load, reload, overrides --------------------------------------------------------


def test_cache_is_lazy_and_reloadable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = home / ".rutherford"
    _write_panels(directory, {"duo": {"targets": [{"cli": "codex"}, {"cli": "kiro"}]}})
    loads = {"count": 0}

    def loader() -> PanelStore:
        loads["count"] += 1
        return load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p")

    cache = PanelCache(loader)
    assert loads["count"] == 0  # nothing read until first use
    assert cache.names() == ["duo"]
    assert loads["count"] == 1
    cache.store()
    assert loads["count"] == 1  # cached, not re-read

    _write_panels(
        directory,
        {
            "duo": {"targets": [{"cli": "codex"}, {"cli": "kiro"}]},
            "trio": {"targets": [{"cli": "codex"}, {"cli": "kiro"}, {"cli": "claude_code"}]},
        },
    )
    cache.reload()
    assert loads["count"] == 2
    assert "trio" in cache.names()  # the edit was picked up


def test_overrides_shallow_merge_over_a_panel(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(
        home / ".rutherford", {"duo": {"description": "orig", "targets": [{"cli": "codex"}, {"cli": "kiro"}]}}
    )
    cache = PanelCache(lambda: load_panels(KNOWN, env=_env(home), cwd=tmp_path / "p"))
    panel = cache.resolve("duo", {"strategy": "majority", "description": "tweaked"})
    assert panel.strategy == "majority"
    assert panel.description == "tweaked"
    assert [target.cli for target in panel.targets] == ["codex", "kiro"]  # targets untouched


# --- tool integration: panel= on consensus / debate / review ------------------------------------


def _duo_store(stance: str | None = None) -> PanelStore:
    """A two-seat panel over fake adapters ``a`` and ``b``, optionally steering ``b``."""
    seat_b = PanelTarget(cli="b", stance=Stance(stance)) if stance else PanelTarget(cli="b")
    return PanelStore({"duo": Panel(name="duo", targets=[PanelTarget(cli="a"), seat_b])})


def _seeded_app(store: PanelStore) -> tuple[Any, FakeProcessRunner]:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b")], runner=runner, panels=PanelCache.seeded(store))
    return app, runner


async def test_consensus_resolves_a_panel_and_applies_stance() -> None:
    app, runner = _seeded_app(_duo_store(stance="against"))
    out = await consensus_tool(app, prompt="rewrite in Rust?", panel="duo")
    assert "voices[2]" in out
    prompts = [spec.argv[2] for spec, _ in runner.calls]
    assert any("Argue against" in prompt for prompt in prompts)  # stance came from the panel seat


async def test_debate_resolves_a_panel() -> None:
    app, _ = _seeded_app(_duo_store())
    out = await debate_tool(app, prompt="q", panel="duo", rounds=1)
    assert "rounds[1]" in out


async def test_review_resolves_a_panel() -> None:
    app, _ = _seeded_app(_duo_store())
    out = await review_tool(app, panel="duo", diff="--- a\n+++ b\n")
    assert "voices[2]" in out


async def test_panel_and_targets_are_mutually_exclusive() -> None:
    app, _ = _seeded_app(_duo_store())
    with pytest.raises(RutherfordError, match="mutually exclusive"):
        await consensus_tool(app, prompt="q", panel="duo", targets=["a"])


async def test_unknown_panel_through_a_tool_errors() -> None:
    app, _ = _seeded_app(PanelStore({}))
    with pytest.raises(RutherfordError) as info:
        await consensus_tool(app, prompt="q", panel="ghost")
    assert info.value.code == "PANEL_NOT_FOUND"


async def test_reload_panels_tool_lists_panels() -> None:
    app, _ = _seeded_app(_duo_store())
    out = await reload_panels_tool(app)
    assert "duo" in out
    assert "count: 1" in out


async def test_panel_strategy_drives_a_parity_pair_escalation() -> None:
    # A roundtable whose strategy is parity-pair: proposer approves, the parity dissenter blocks,
    # so the panel must escalate. The strategy comes from the panel, not the call.
    store = PanelStore(
        {
            "roundtable": Panel(
                name="roundtable",
                strategy="parity-pair",
                targets=[PanelTarget(cli="a", label="proposer"), PanelTarget(cli="b", label="dissenter", parity=True)],
            )
        }
    )

    def run_fn(spec: Any) -> ProcessResult:
        verdict = "approve" if spec.argv[0] == "a" else "block"
        return ProcessResult(exit_code=0, stdout=f"reasoning\nVERDICT: {verdict}")

    runner = FakeProcessRunner(run_fn=run_fn)
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b")], runner=runner, panels=PanelCache.seeded(store))
    out = await consensus_tool(app, prompt="is this a primitive?", panel="roundtable")
    assert "strategy: parity-pair" in out
    assert "outcome: escalate" in out
