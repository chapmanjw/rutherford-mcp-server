# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the role loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.domain.errors import RutherfordError
from rutherford.services.roles import load_roles


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
