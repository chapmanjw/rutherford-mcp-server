# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for saved panels: loading, scope merging, resolution + overrides, errors, and the tools.

A panel is a named consensus/debate roster stored as ``panels.toon`` and discovered across the same scopes
as the rest of the config (``~/.rutherford`` -> project ``<cwd>/.rutherford`` -> ``$RUTHERFORD_CONFIG_DIR``),
the closest scope winning. These exercise the loader, the cache + override path, the PANEL_NOT_FOUND /
PANEL_INVALID errors, the ``reload_panels`` tool shape, and panel resolution into a consensus / debate
request via the tool layer (driving the fake ACP agent end to end).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.panels import Panel, PanelCache, PanelStore, PanelTarget, load_panels
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import Effort, Stance, Strategy
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import Target
from rutherford.io.serialize import decode, encode
from rutherford.tools.consensus import consensus_tool
from rutherford.tools.debate import debate_tool
from rutherford.tools.panels import panel_for_call, reload_panels_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
FAKE_A = AgentDescriptor(
    "fake_a",
    "Fake A",
    _FAKE_CMD,
    provider="alpha",
    default_model="model-a",
    env_overrides=(("RUTHERFORD_FAKE_MODELS", "model-a"),),
)
FAKE_B = AgentDescriptor(
    "fake_b",
    "Fake B",
    _FAKE_CMD,
    provider="beta",
    default_model="model-b",
    env_overrides=(("RUTHERFORD_FAKE_MODELS", "model-b"),),
)

#: The agent ids the panel loader validates targets against in these tests.
KNOWN = ["fake", "fake_a", "fake_b"]


def _registry() -> DescriptorRegistry:
    return DescriptorRegistry([FAKE, FAKE_A, FAKE_B])


def _write_panels(directory: Path, panels: dict[str, dict[str, Any]]) -> None:
    """Write a ``panels.toon`` under ``directory/.rutherford`` from a plain panels mapping (valid TOON)."""
    base = directory / ".rutherford"
    base.mkdir(parents=True, exist_ok=True)
    (base / "panels.toon").write_text(encode({"panels": panels}), encoding="utf-8")


# --- loader: discovery, validation, and scope merge --------------------------


def test_no_panels_file_is_an_empty_store(tmp_path: Path) -> None:
    store = load_panels(KNOWN, env={"USERPROFILE": str(tmp_path / "home")}, cwd=str(tmp_path / "proj"))
    assert store.names() == []


def test_loads_a_panel_with_its_targets_and_strategy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(
        home,
        {
            "crew": {
                "description": "the usual crew",
                "strategy": "majority",
                "targets": [{"cli": "fake"}, {"cli": "fake_a", "stance": "against", "weight": 2}],
            }
        },
    )
    store = load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert store.names() == ["crew"]
    panel = store.get("crew")
    assert panel.description == "the usual crew"
    assert panel.strategy is Strategy.MAJORITY
    targets = panel.to_targets()
    assert [t.cli for t in targets] == ["fake", "fake_a"]
    assert targets[1].stance is Stance.AGAINST
    assert targets[1].effective_weight == 2.0


def test_loads_per_seat_effort_onto_the_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(
        home,
        {
            "crew": {
                "targets": [
                    {"cli": "fake", "effort": "xhigh"},
                    {"cli": "fake_a", "effort": "high"},
                    {"cli": "fake_b"},  # no per-seat effort -> None (inherits the call/config)
                ]
            }
        },
    )
    store = load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    targets = store.get("crew").to_targets()
    assert targets[0].effort is Effort.XHIGH
    assert targets[1].effort is Effort.HIGH
    assert targets[2].effort is None


def test_unknown_effort_is_panel_invalid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": [{"cli": "fake", "effort": "turbo"}]}})
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert exc.value.code is ErrorCode.PANEL_INVALID
    assert "unknown effort" in str(exc.value)


def test_project_scope_overrides_user_scope_by_name(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    _write_panels(home, {"crew": {"description": "global", "targets": [{"cli": "fake"}]}})
    _write_panels(proj, {"crew": {"description": "project", "targets": [{"cli": "fake_a"}, {"cli": "fake_b"}]}})
    store = load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(proj))
    panel = store.get("crew")
    assert panel.description == "project"  # the closer scope won
    assert [t.cli for t in panel.to_targets()] == ["fake_a", "fake_b"]


def test_env_config_dir_overrides_both(tmp_path: Path) -> None:
    home, proj, env_dir = tmp_path / "home", tmp_path / "proj", tmp_path / "explicit"
    _write_panels(home, {"crew": {"description": "global", "targets": [{"cli": "fake"}]}})
    _write_panels(proj, {"crew": {"description": "project", "targets": [{"cli": "fake"}]}})
    # RUTHERFORD_CONFIG_DIR points at the directory itself (no `.rutherford` suffix), so write there directly.
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "panels.toon").write_text(
        encode({"panels": {"crew": {"description": "explicit", "targets": [{"cli": "fake_b"}]}}}), encoding="utf-8"
    )
    store = load_panels(
        KNOWN,
        env={"USERPROFILE": str(home), "RUTHERFORD_CONFIG_DIR": str(env_dir)},
        cwd=str(proj),
    )
    assert store.get("crew").description == "explicit"


def test_distinct_panels_across_scopes_all_load(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    _write_panels(home, {"global-only": {"targets": [{"cli": "fake"}]}})
    _write_panels(proj, {"project-only": {"targets": [{"cli": "fake_a"}]}})
    store = load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(proj))
    assert store.names() == ["global-only", "project-only"]


# --- loader: PANEL_INVALID -----------------------------------------------------


def test_unknown_cli_is_panel_invalid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": [{"cli": "fake"}, {"cli": "ghost"}]}})
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert exc.value.code is ErrorCode.PANEL_INVALID
    assert "ghost" in exc.value.message


def test_unknown_strategy_is_panel_invalid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"strategy": "telepathy", "targets": [{"cli": "fake"}]}})
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert exc.value.code is ErrorCode.PANEL_INVALID
    assert "telepathy" in exc.value.message


def test_empty_targets_is_panel_invalid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": []}})
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert exc.value.code is ErrorCode.PANEL_INVALID


def test_negative_weight_is_panel_invalid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": [{"cli": "fake", "weight": -1}]}})
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert exc.value.code is ErrorCode.PANEL_INVALID
    assert "non-negative" in exc.value.message


def test_unknown_panel_key_is_panel_invalid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": [{"cli": "fake"}], "rounds": 3}})  # rounds is not a panel key
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    assert exc.value.code is ErrorCode.PANEL_INVALID
    assert "rounds" in exc.value.message


def test_all_problems_reported_in_one_pass(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"strategy": "bogus", "targets": [{"cli": "ghost"}]}})
    with pytest.raises(RutherfordError) as exc:
        load_panels(KNOWN, env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    # Both the bad strategy and the bad cli are named -- one aggregated report, not a fail-on-first.
    assert "bogus" in exc.value.message and "ghost" in exc.value.message


# --- store / cache lookup ------------------------------------------------------


def test_get_unknown_panel_raises_panel_not_found() -> None:
    store = PanelStore({"crew": Panel(name="crew", targets=[])})
    with pytest.raises(RutherfordError) as exc:
        store.get("nope")
    assert exc.value.code is ErrorCode.PANEL_NOT_FOUND
    assert "crew" in exc.value.message  # lists what is available


def test_cache_resolve_applies_overrides() -> None:
    panel = Panel(name="crew", strategy=Strategy.ALL_VOICES, targets=[PanelTarget(cli="fake")])
    cache = PanelCache.seeded(PanelStore({"crew": panel}))
    overridden = cache.resolve("crew", {"strategy": "majority"})
    assert overridden.strategy is Strategy.MAJORITY
    assert overridden.name == "crew"  # the merge preserved the rest
    assert [t.cli for t in overridden.to_targets()] == ["fake"]  # the unchanged seats survive the merge


def test_cache_resolve_bad_override_is_panel_invalid() -> None:
    panel = Panel(name="crew", targets=[PanelTarget(cli="fake")])
    cache = PanelCache.seeded(PanelStore({"crew": panel}))
    with pytest.raises(RutherfordError) as exc:
        cache.resolve("crew", {"strategy": "telepathy"})  # an invalid strategy fails re-validation
    assert exc.value.code is ErrorCode.PANEL_INVALID


# --- reload_panels tool --------------------------------------------------------


async def test_reload_panels_tool_shape(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"description": "the crew", "targets": [{"cli": "fake"}, {"cli": "fake_a"}]}})
    app = _app(env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    data = decode(await reload_panels_tool(app))
    assert data["reloaded"] is True
    assert data["count"] == 1
    entry = data["panels"][0]
    assert entry["name"] == "crew"
    assert entry["description"] == "the crew"
    assert entry["target_count"] == 2


# --- panel_for_call guards -----------------------------------------------------


def test_panel_with_targets_is_invalid_input(tmp_path: Path) -> None:
    # The mutual-exclusion guard fires BEFORE the store is touched, so an empty scope is enough.
    app = _app(env={"USERPROFILE": str(tmp_path / "home")}, cwd=str(tmp_path / "proj"))
    with pytest.raises(RutherfordError) as exc:
        panel_for_call(app, "crew", None, [Target(cli="fake")], None)
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_panel_with_stances_is_invalid_input(tmp_path: Path) -> None:
    app = _app(env={"USERPROFILE": str(tmp_path / "home")}, cwd=str(tmp_path / "proj"))
    with pytest.raises(RutherfordError) as exc:
        panel_for_call(app, "crew", None, None, ["for"])
    assert exc.value.code is ErrorCode.INVALID_INPUT


# --- panel resolved into a consensus / debate run ------------------------------


def _app(env: dict[str, str] | None = None, cwd: str | None = None) -> AppContext:
    """An app whose panel cache loads from the given scopes (defaults to empty tmp scopes)."""
    config = RutherfordConfig()
    app = build_app_context(config=config, descriptors=_registry())
    # Re-point the panel cache at the test scopes so a fixture panels.toon is what loads.
    app.panels = PanelCache(lambda: load_panels(KNOWN, env=env, cwd=cwd))
    return app


async def test_consensus_resolves_a_panel_and_adopts_its_strategy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"strategy": "majority", "targets": [{"cli": "fake"}, {"cli": "fake_a"}]}})
    app = _app(env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    # Both panel seats plant ``VERDICT: yes``: this proves the panel resolved into the two seats AND that the
    # panel's majority strategy was adopted with no explicit ``strategy`` on the call (a StrategyResult, not the
    # all-voices shape).
    out = await consensus_tool(app, prompt="Decide.\nSAY=VERDICT: yes", panel="crew", working_dir=str(REPO_ROOT))
    assert "strategy: majority" in out  # the panel's strategy was adopted
    assert "decision: yes" in out
    assert "cli: fake\n" in out and "fake_a" in out  # both panel seats took part


async def test_consensus_unknown_panel_is_panel_not_found(tmp_path: Path) -> None:
    app = _app(env={"USERPROFILE": str(tmp_path / "home")}, cwd=str(tmp_path / "proj"))
    with pytest.raises(RutherfordError) as exc:
        await consensus_tool(app, prompt="x", panel="ghost", working_dir=str(REPO_ROOT))
    assert exc.value.code is ErrorCode.PANEL_NOT_FOUND


async def test_consensus_panel_and_targets_mutually_exclusive(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": [{"cli": "fake"}]}})
    app = _app(env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    with pytest.raises(RutherfordError) as exc:
        await consensus_tool(app, prompt="x", panel="crew", targets=["fake"], working_dir=str(REPO_ROOT))
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_debate_resolves_a_panel(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_panels(home, {"crew": {"targets": [{"cli": "fake"}, {"cli": "fake_a"}]}})
    app = _app(env={"USERPROFILE": str(home)}, cwd=str(tmp_path / "proj"))
    out = await debate_tool(app, prompt="Argue.", panel="crew", rounds=1, synthesize=False, working_dir=str(REPO_ROOT))
    data = decode(out)
    # A one-round debate over the two panel seats: round one has both voices.
    assert len(data["rounds"][0]["contributions"]) == 2
