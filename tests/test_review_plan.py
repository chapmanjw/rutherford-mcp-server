# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the review and plan tools (built on consensus and delegate)."""

from __future__ import annotations

import pytest

from rutherford.config.schema import RutherfordConfig
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ProcessResult
from rutherford.tools.plan import plan_tool
from rutherford.tools.review import review_tool
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app


async def test_review_with_diff_returns_voices() -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="looks good")),
    )
    out = await review_tool(app, targets=[{"cli": "a"}, {"cli": "b"}], diff="- old\n+ new")
    assert "voices[2]" in out


async def test_review_with_paths_uses_codereviewer_role() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    app = make_app(adapters=[FakeAdapter("a")], runner=runner)
    out = await review_tool(app, targets=[{"cli": "a"}], paths=["src/x.py"])
    assert "voices[1]" in out
    spec, _ = runner.calls[0]
    # The codereviewer preamble and the file list both reach the invocation.
    assert "code reviewer" in spec.argv[2].lower()
    assert "src/x.py" in spec.argv[2]


async def test_review_requires_diff_or_paths() -> None:
    app = make_app(adapters=[FakeAdapter("a")])
    with pytest.raises(RutherfordError, match="diff"):
        await review_tool(app, targets=[{"cli": "a"}])


async def test_plan_uses_planner_role() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="1. scaffold\n2. test"))
    app = make_app(adapters=[FakeAdapter("a")], runner=runner)
    out = await plan_tool(app, cli="a", goal="ship the feature")
    assert "ok: true" in out
    spec, _ = runner.calls[0]
    assert "planning specialist" in spec.argv[2]


async def test_review_is_clamped_to_read_only() -> None:
    # review documents itself read-only and now enforces it: no safety_mode parameter exists, and
    # the delegations run read_only regardless of any configured default. The config default is
    # deliberately set to WRITE here -- the clamp's whole claim is that it wins over config, and a
    # stock read_only default would let a regression through resolve_safety_mode pass unnoticed.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(default_safety_mode="write")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("a")], runner=runner, config=config)
    await review_tool(app, targets=[{"cli": "a"}], diff="- old\n+ new")
    spec, _ = runner.calls[0]
    assert "--safety=read_only" in spec.argv  # NOT the configured write default
    with pytest.raises(TypeError):  # the parameter is gone, not silently accepted
        await review_tool(app, targets=[{"cli": "a"}], diff="x", safety_mode="write")  # type: ignore[call-arg]


async def test_plan_is_clamped_to_read_only() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="plan"))
    config = RutherfordConfig(default_safety_mode="yolo")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("a")], runner=runner, config=config)
    await plan_tool(app, cli="a", goal="g")
    spec, _ = runner.calls[0]
    assert "--safety=read_only" in spec.argv  # NOT the configured yolo default
    with pytest.raises(TypeError):
        await plan_tool(app, cli="a", goal="g", safety_mode="yolo")  # type: ignore[call-arg]
