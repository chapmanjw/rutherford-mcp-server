# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for role personas: built-in loading, role_dirs override, tolerant parsing, apply, and the tools."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import build_app_context
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import decode
from rutherford.services.roles import RoleStore, _parse_frontmatter, _role_from_text
from rutherford.tools.common import apply_role
from rutherford.tools.delegate import delegate_tool
from rutherford.tools.roles import list_roles_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = AgentDescriptor("fake", "Fake", (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")))

#: The five built-in role ids shipped under src/rutherford/roles/.
BUILTIN_IDS = {"principal-reviewer", "architect", "debugger", "security-reviewer", "explainer"}


def _app(role_dirs: list[str] | None = None):
    config = RutherfordConfig(role_dirs=role_dirs or [])
    return build_app_context(config=config, descriptors=DescriptorRegistry([FAKE]))


# --- built-in loading --------------------------------------------------------


def test_builtins_load_with_expected_ids_and_metadata() -> None:
    store = RoleStore()
    ids = {role.id for role in store.list()}
    assert ids >= BUILTIN_IDS
    assert len(store.list()) >= 5
    reviewer = store.get("principal-reviewer")
    assert reviewer.name == "Principal Reviewer"
    assert "rigorous" in reviewer.description.lower()
    assert reviewer.prompt  # the markdown body is the system prompt
    assert "frontmatter" not in reviewer.prompt.lower()  # the --- block is stripped from the body


def test_list_is_sorted_by_id() -> None:
    ids = [role.id for role in RoleStore().list()]
    assert ids == sorted(ids)


# --- role_dirs loading + override --------------------------------------------


def test_role_dir_adds_a_new_role(tmp_path: Path) -> None:
    (tmp_path / "house.md").write_text(
        "---\nname: House Style\ndescription: The house reviewer.\n---\nFollow the house standard.\n",
        encoding="utf-8",
    )
    store = RoleStore(role_dirs=[str(tmp_path)])
    assert store.has("house")
    house = store.get("house")
    assert house.name == "House Style"
    assert house.prompt == "Follow the house standard."


def test_role_dir_overrides_a_builtin(tmp_path: Path) -> None:
    (tmp_path / "debugger.md").write_text(
        "---\nname: Custom Debugger\ndescription: A workspace override.\n---\nCUSTOM-DEBUG-BODY\n",
        encoding="utf-8",
    )
    store = RoleStore(role_dirs=[str(tmp_path)])
    debugger = store.get("debugger")
    assert debugger.name == "Custom Debugger"
    assert debugger.prompt == "CUSTOM-DEBUG-BODY"


def test_missing_role_dir_is_skipped_not_fatal(tmp_path: Path) -> None:
    missing = str(tmp_path / "does-not-exist")
    store = RoleStore(role_dirs=[missing])  # no raise
    assert {role.id for role in store.list()} >= BUILTIN_IDS


# --- tolerant parsing --------------------------------------------------------


def test_malformed_role_file_is_skipped(tmp_path: Path) -> None:
    # An empty body (frontmatter only) is skipped; a no-frontmatter file still loads with defaults.
    (tmp_path / "empty.md").write_text("---\nname: Empty\n---\n", encoding="utf-8")
    (tmp_path / "bare.md").write_text("Just a body, no frontmatter at all.", encoding="utf-8")
    store = RoleStore(role_dirs=[str(tmp_path)])
    assert not store.has("empty")  # no prompt body -> skipped, never fatal
    assert store.has("bare")
    bare = store.get("bare")
    assert bare.name == "bare"  # defaulted from the id
    assert bare.description == "Just a body, no frontmatter at all."  # defaulted from the first line


def test_unterminated_frontmatter_keeps_the_body() -> None:
    meta, body = _parse_frontmatter("---\nname: X\nno closing fence\nstill body")
    assert meta == {}
    assert body == "---\nname: X\nno closing fence\nstill body"


def test_role_from_text_defaults_when_frontmatter_absent() -> None:
    role = _role_from_text("solo", "First line.\nSecond line.")
    assert role.name == "solo"
    assert role.description == "First line."
    assert role.prompt == "First line.\nSecond line."


def test_frontmatter_tolerates_bom_and_blank_lines(tmp_path: Path) -> None:
    (tmp_path / "bom.md").write_text(
        "﻿---\nname: Bom\n\ndescription: With a BOM.\n---\nBody after BOM.\n", encoding="utf-8"
    )
    store = RoleStore(role_dirs=[str(tmp_path)])
    bom = store.get("bom")
    assert bom.name == "Bom"
    assert bom.description == "With a BOM."
    assert bom.prompt == "Body after BOM."


# --- apply + lookup errors ---------------------------------------------------


def test_apply_prepends_role_then_delimiter_then_prompt() -> None:
    store = RoleStore()
    composed = store.apply("explainer", "Explain quicksort.")
    role = store.get("explainer")
    assert composed == f"{role.prompt}\n\n---\n\nExplain quicksort."
    assert composed.startswith(role.prompt)
    assert composed.endswith("Explain quicksort.")


def test_get_unknown_role_raises_unknown_role() -> None:
    store = RoleStore()
    with pytest.raises(RutherfordError) as exc:
        store.get("nope")
    assert exc.value.code is ErrorCode.UNKNOWN_ROLE
    assert "principal-reviewer" in exc.value.message  # lists the known ids


def test_apply_role_helper_no_op_when_none() -> None:
    store = RoleStore()
    assert apply_role(store, None, "untouched") == "untouched"


def test_apply_role_helper_raises_on_bad_id() -> None:
    store = RoleStore()
    with pytest.raises(RutherfordError) as exc:
        apply_role(store, "ghost", "x")
    assert exc.value.code is ErrorCode.UNKNOWN_ROLE


# --- list_roles tool ---------------------------------------------------------


async def test_list_roles_tool_shape() -> None:
    data = decode(await list_roles_tool(_app()))
    roles = data["roles"]
    by_id = {entry["id"]: entry for entry in roles}
    assert set(by_id) >= BUILTIN_IDS
    reviewer = by_id["principal-reviewer"]
    assert set(reviewer) == {"id", "name", "description"}  # no prompt body in the listing
    assert reviewer["name"] == "Principal Reviewer"


# --- tool-level role injection -----------------------------------------------


async def test_delegate_injects_the_role_prefix(tmp_path: Path) -> None:
    # The fake agent echoes ECHO:<first 40 chars of the composed prompt>. A role_dirs role whose body
    # begins with a short unique marker lands that marker at the very front of the composed prompt, so the
    # echo proves the role text was prepended ahead of the user task.
    (tmp_path / "marker.md").write_text(
        "---\nname: Marker\ndescription: A test marker role.\n---\nROLEMARKER-XYZ persona instructions.\n",
        encoding="utf-8",
    )
    app = _app(role_dirs=[str(tmp_path)])
    out = await delegate_tool(app, cli="fake", prompt="the user task", role="marker", working_dir=str(REPO_ROOT))
    assert "ROLEMARKER-XYZ" in out  # the role body led the prompt the agent received


async def test_delegate_unknown_role_raises_before_running() -> None:
    with pytest.raises(RutherfordError) as exc:
        await delegate_tool(_app(), cli="fake", prompt="x", role="missing", working_dir=str(REPO_ROOT))
    assert exc.value.code is ErrorCode.UNKNOWN_ROLE


async def test_delegate_without_role_is_unchanged() -> None:
    out = await delegate_tool(_app(), cli="fake", prompt="what is 17 + 25?", working_dir=str(REPO_ROOT))
    assert "42" in out  # no role -> the bare prompt reaches the agent
