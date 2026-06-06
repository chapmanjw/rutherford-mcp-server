# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the role loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import encode
from rutherford.services.roles import load_roles


def _env(home: Path, config_dir: Path | None = None) -> dict[str, str]:
    env = {"USERPROFILE": str(home), "HOME": str(home)}
    if config_dir is not None:
        env["RUTHERFORD_CONFIG_DIR"] = str(config_dir)
    return env


def _write_md_role(directory: Path, name: str, body: str, **meta: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    front = "".join(f"{key}: {value}\n" for key, value in meta.items())
    text = f"---\n{front}---\n{body}\n" if meta else f"{body}\n"
    (directory / f"{name}.md").write_text(text, encoding="utf-8")


def _write_toon_role(directory: Path, name: str, system_prompt: str, **meta: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"name": name, "system_prompt": system_prompt, **meta}
    (directory / f"{name}.toon").write_text(encode(record), encoding="utf-8")


def test_builtin_roles_present() -> None:
    store = load_roles()
    assert set(store.names()) >= {"planner", "codereviewer", "security", "debugger"}


def test_builtin_role_has_preamble_and_metadata() -> None:
    planner = load_roles().get("planner")
    assert planner.display_name == "Planner"
    assert planner.description
    assert "plan" in planner.preamble.lower()
    # Frontmatter must not bleed into the preamble.
    assert not planner.preamble.startswith("---")


def test_unknown_role_raises() -> None:
    with pytest.raises(RutherfordError, match="unknown role"):
        load_roles().get("nonexistent")


def test_extra_dir_overrides_and_adds(tmp_path: Path) -> None:
    (tmp_path / "planner.md").write_text(
        "---\nname: planner\ndisplay_name: Custom Planner\n---\nDo it my way.\n",
        encoding="utf-8",
    )
    (tmp_path / "researcher.md").write_text(
        "---\nname: researcher\ndescription: Investigates.\n---\nResearch the topic.\n",
        encoding="utf-8",
    )
    store = load_roles(extra_dirs=[tmp_path])
    assert store.get("planner").display_name == "Custom Planner"
    assert store.get("planner").preamble == "Do it my way."
    assert store.has("researcher")
    assert store.get("researcher").description == "Investigates."


def test_role_without_frontmatter_uses_filename(tmp_path: Path) -> None:
    (tmp_path / "blunt.md").write_text("Just the body.", encoding="utf-8")
    role = load_roles(extra_dirs=[tmp_path]).get("blunt")
    assert role.display_name == "Blunt"
    assert role.preamble == "Just the body."


def test_missing_extra_dir_is_ignored(tmp_path: Path) -> None:
    store = load_roles(extra_dirs=[tmp_path / "does-not-exist"])
    assert store.has("planner")


# --- source provenance + scope layering ---------------------------------------------------------


def test_builtin_roles_carry_a_builtin_source(tmp_path: Path) -> None:
    store = load_roles(env=_env(tmp_path / "nohome"), cwd=tmp_path / "noproj")
    assert all(role.source == "builtin" for role in store.all())
    assert store.get("planner").source == "builtin"


def test_well_known_scopes_layer_with_closest_winning(tmp_path: Path) -> None:
    home, proj = tmp_path / "home", tmp_path / "proj"
    _write_md_role(home / ".rutherford" / "roles", "planner", "home planner", display_name="Home Planner")
    _write_md_role(home / ".rutherford" / "roles", "homeonly", "only at home")
    _write_md_role(proj / ".rutherford" / "roles", "planner", "project planner", display_name="Project Planner")
    store = load_roles(env=_env(home), cwd=proj)
    assert store.get("planner").preamble == "project planner"  # project overrides home
    assert store.get("planner").source == "project"
    assert store.get("homeonly").source == "user"  # home-scope role still present


def test_config_dir_scope_overrides_project(tmp_path: Path) -> None:
    home, proj, cfg = tmp_path / "home", tmp_path / "proj", tmp_path / "cfg"
    _write_md_role(proj / ".rutherford" / "roles", "planner", "project planner")
    _write_md_role(cfg / "roles", "planner", "env planner")
    store = load_roles(env=_env(home, cfg), cwd=proj)
    assert store.get("planner").preamble == "env planner"
    assert store.get("planner").source == "env"


def test_config_role_dir_has_config_source(tmp_path: Path) -> None:
    _write_md_role(tmp_path / "myroles", "researcher", "Research the topic.", description="Investigates.")
    store = load_roles(extra_dirs=[tmp_path / "myroles"], env=_env(tmp_path / "nohome"), cwd=tmp_path / "noproj")
    assert store.get("researcher").source == "config"


# --- TOON role files + malformed-file skip ------------------------------------------------------


def test_toon_role_file_loads(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_toon_role(
        home / ".rutherford" / "roles",
        "auditor",
        "Audit every citation carefully.",
        display_name="Citation Auditor",
        description="Checks sources.",
    )
    role = load_roles(env=_env(home), cwd=tmp_path / "p").get("auditor")
    assert role.preamble == "Audit every citation carefully."
    assert role.display_name == "Citation Auditor"
    assert role.description == "Checks sources."
    assert role.source == "user"


def test_malformed_role_files_are_skipped_not_fatal(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    home = tmp_path / "home"
    roles_dir = home / ".rutherford" / "roles"
    roles_dir.mkdir(parents=True)
    (roles_dir / "broken.toon").write_text("items[3]: 1,2\n", encoding="utf-8")  # invalid TOON
    (roles_dir / "empty.md").write_text("---\nname: empty\n---\n\n", encoding="utf-8")  # no body
    _write_md_role(roles_dir, "good", "A usable persona.")
    with caplog.at_level("WARNING"):
        store = load_roles(env=_env(home), cwd=tmp_path / "p")
    assert not store.has("broken")
    assert not store.has("empty")
    assert store.has("good")  # a good role beside the bad ones still loads
    assert store.has("planner")  # built-ins still loaded -- the server did not crash
    assert any("skipping malformed role" in record.message for record in caplog.records)
