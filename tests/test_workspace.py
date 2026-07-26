# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the read-only workspace-breadth check.

This lives apart from ``config.trust`` on purpose -- ``tools/setup.py`` is model-callable and may import
this, while a total ban keeps it away from the allowlist EDITOR. ``test_trust.py`` enforces that ban.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rutherford.config.workspace import breadth_warning


def test_a_filesystem_root_is_flagged() -> None:
    root = Path(Path.cwd().anchor)
    warning = breadth_warning(root)
    assert warning is not None and "root" in warning


def test_the_home_directory_is_flagged() -> None:
    warning = breadth_warning(Path.home())
    assert warning is not None and "home directory" in warning


def test_the_parent_of_all_homes_is_flagged() -> None:
    """``/home``, ``/Users``, ``C:\\Users`` -- a container of every user's tree, not one project."""
    parent = Path.home().resolve().parent
    if parent.parent == parent:  # a home directly under the root would hit the root branch instead
        pytest.skip("home sits directly under the filesystem root on this machine")
    warning = breadth_warning(parent)
    assert warning is not None
    assert "every user's home directory" in warning or "top-level directory" in warning


def test_a_shallow_top_level_directory_is_flagged(tmp_path: Path, monkeypatch: Any) -> None:
    """A one-segment path such as /opt or C:\\repo, with home pointed elsewhere so that branch is skipped."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    shallow = Path(Path.cwd().anchor) / "opt"
    warning = breadth_warning(shallow)
    assert warning is not None and "top-level directory" in warning


def test_an_ordinary_project_is_silent(tmp_path: Path) -> None:
    """tmp_path is several levels deep, which is what a real checkout looks like."""
    project = tmp_path / "projects" / "myapp"
    project.mkdir(parents=True)
    assert breadth_warning(project) is None


def test_an_unresolvable_path_is_silent(monkeypatch: Any) -> None:
    """A path the OS refuses to resolve tells us nothing, so it must not manufacture a warning."""

    def _boom(self: Path) -> Path:
        raise OSError("cannot resolve")

    monkeypatch.setattr(Path, "resolve", _boom)
    assert breadth_warning("/somewhere") is None


def test_an_undeterminable_home_does_not_break_the_check(tmp_path: Path, monkeypatch: Any) -> None:
    """``Path.home()`` raises when the environment has no home; the depth check must still run.

    A container or a service account can hit this, and losing the whole warning there would be worse
    than losing only the home-specific wording.
    """

    def _no_home(cls: type[Path]) -> Path:
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))

    deep = tmp_path / "projects" / "myapp"
    deep.mkdir(parents=True)
    assert breadth_warning(deep) is None  # still silent for a normal project

    shallow = Path(Path.cwd().anchor) / "opt"
    warning = breadth_warning(shallow)
    assert warning is not None and "top-level directory" in warning
