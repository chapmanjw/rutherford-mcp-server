# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the thin tool layer (delegate, consensus, jobs)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from toon import decode

from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import AuthState
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import ProcessResult
from rutherford.tools.consensus import consensus_tool
from rutherford.tools.debate import debate_tool
from rutherford.tools.delegate import delegate_tool
from rutherford.tools.jobs import job_result_tool, job_status_tool
from tests.fakes import FakeAdapter, FakeProcessRunner, make_app


def _decode(toon_text: str) -> Any:
    return decode(toon_text)


async def test_delegate_tool_sync_returns_envelope() -> None:
    app = make_app(
        adapters=[FakeAdapter("fake")], runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="answer"))
    )
    out = await delegate_tool(app, cli="fake", prompt="question")
    data = _decode(out)
    assert data["ok"] is True
    assert data["text"] == "answer"
    assert data["target"]["cli"] == "fake"


async def test_delegate_tool_unknown_target_is_failed_result() -> None:
    app = make_app(adapters=[FakeAdapter("fake")])
    out = await delegate_tool(app, cli="ghost", prompt="q")
    data = _decode(out)
    assert data["ok"] is False
    assert data["error"]["code"] == "UNKNOWN_TARGET"


async def test_delegate_tool_bad_safety_mode_raises() -> None:
    app = make_app(adapters=[FakeAdapter("fake")])
    with pytest.raises(RutherfordError, match="safety_mode"):
        await delegate_tool(app, cli="fake", prompt="q", safety_mode="bogus")


async def test_omitted_safety_mode_applies_the_configured_default() -> None:
    # The documented contract for default_safety_mode: it fills the field ONLY when the caller
    # omits it. propose is non-mutating, so no trusted-workspace gate interferes here.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(default_safety_mode="propose")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("fake")], runner=runner, config=config)
    out = await delegate_tool(app, cli="fake", prompt="q")
    assert _decode(out)["safety_mode"] == "propose"
    spec, _ = runner.calls[0]
    assert "--safety=propose" in spec.argv  # the mode actually reached the adapter


async def test_explicit_safety_mode_beats_the_configured_default() -> None:
    # An explicit read_only is a choice, not an omission -- config must not override it.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(default_safety_mode="propose")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("fake")], runner=runner, config=config)
    out = await delegate_tool(app, cli="fake", prompt="q", safety_mode="read_only")
    assert _decode(out)["safety_mode"] == "read_only"


async def test_consensus_omitted_safety_mode_applies_the_configured_default() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(default_safety_mode="propose")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b")], runner=runner, config=config)
    await consensus_tool(app, targets=["a", "b"], prompt="q")
    # Every fanned-out delegation carried the configured default to its adapter.
    assert len(runner.calls) == 2
    assert all("--safety=propose" in spec.argv for spec, _ in runner.calls)


async def test_config_defaulted_write_still_hits_the_trusted_workspace_gate() -> None:
    # The safety-critical composition: a mutating mode arriving via the CONFIG default (not an
    # explicit argument) must still be stopped by the trusted-workspace gate. If the resolved
    # default bypassed the gate, setting default_safety_mode="write" would silently grant every
    # omitted-mode call write access anywhere.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    config = RutherfordConfig(default_safety_mode="write")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("fake")], runner=runner, config=config)
    out = await delegate_tool(app, cli="fake", prompt="q", working_dir="/untrusted/dir")
    data = _decode(out)
    assert data["ok"] is False
    assert data["error"]["code"] == "WORKSPACE_NOT_TRUSTED"
    assert runner.calls == []  # nothing spawned


async def test_debate_omitted_safety_mode_applies_the_configured_default() -> None:
    # The third of the three tools carrying the None sentinel; each resolves independently, so
    # each needs its own pin (delegate and consensus alone would not catch a debate regression).
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="position"))
    config = RutherfordConfig(default_safety_mode="propose")  # type: ignore[arg-type]
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b")], runner=runner, config=config)
    await debate_tool(app, targets=["a", "b"], prompt="q", rounds=1, synthesize=False)
    assert len(runner.calls) == 2
    assert all("--safety=propose" in spec.argv for spec, _ in runner.calls)


async def test_consensus_tool_unknown_target_is_a_clean_boundary_error() -> None:
    # An unknown CLI in a fan-out tool is one clean UNKNOWN_TARGET, not a buried failed voice.
    app = make_app(adapters=[FakeAdapter("fake")])
    with pytest.raises(RutherfordError) as info:
        await consensus_tool(app, targets=[{"cli": "fake"}, {"cli": "ghost"}], prompt="q")
    assert info.value.code == "UNKNOWN_TARGET"


async def test_consensus_tool_unknown_judge_is_a_clean_boundary_error() -> None:
    # A typo'd judge is a clean UNKNOWN_TARGET, not a silent no-synthesis.
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b")])
    with pytest.raises(RutherfordError) as info:
        await consensus_tool(
            app, targets=[{"cli": "a"}, {"cli": "b"}], prompt="q", synthesize=True, judge={"cli": "ghost"}
        )
    assert info.value.code == "UNKNOWN_TARGET"


async def test_debate_tool_unknown_judge_is_a_clean_boundary_error() -> None:
    app = make_app(adapters=[FakeAdapter("a"), FakeAdapter("b")])
    with pytest.raises(RutherfordError) as info:
        await debate_tool(app, prompt="q", targets=["a", "b"], judge={"cli": "ghost"})
    assert info.value.code == "UNKNOWN_TARGET"


async def test_delegate_tool_async_returns_job_then_result() -> None:
    app = make_app(
        adapters=[FakeAdapter("fake")], runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="bg-answer"))
    )
    submitted = _decode(await delegate_tool(app, cli="fake", prompt="q", mode="async"))
    job_id = submitted["job_id"]

    for _ in range(500):
        status = _decode(await job_status_tool(app, job_id=job_id))
        if status["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0)

    result = _decode(await job_result_tool(app, job_id=job_id))
    assert result["ok"] is True
    assert result["text"] == "bg-answer"


async def test_consensus_tool_returns_voices() -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await consensus_tool(app, targets=[{"cli": "a"}, {"cli": "b"}], prompt="best editor?")
    # The consensus envelope is a TOON array of non-uniform voice objects; assert on the encoded
    # text (python-toon's decoder does not round-trip nested object arrays, but the server only
    # ever encodes, and the output is what an LLM client reads).
    assert "voices[2]" in out
    assert "cli: a" in out
    assert "cli: b" in out
    assert out.count("ok: true") == 2


async def test_consensus_tool_target_cap_raises() -> None:
    app = make_app(
        adapters=[FakeAdapter("a")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
        config=RutherfordConfig(max_targets=1),
    )
    with pytest.raises(RutherfordError, match="cap"):
        await consensus_tool(app, targets=[{"cli": "a"}, {"cli": "a"}], prompt="q")


async def test_consensus_tool_expands_when_targets_omitted() -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await consensus_tool(app, prompt="best editor?")  # no targets -> full authenticated panel
    assert "voices[2]" in out
    assert "cli: a" in out and "cli: b" in out


async def test_consensus_tool_all_sentinel_expands_and_reports_skips() -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b", auth_state=AuthState.NEEDS_LOGIN)],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await consensus_tool(app, targets="all", prompt="q")
    assert "voices[1]" in out  # only the authenticated adapter answers
    assert "skipped" in out and "b" in out  # the skipped adapter is reported


async def test_consensus_tool_empty_list_expands() -> None:
    app = make_app(
        adapters=[FakeAdapter("a")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await consensus_tool(app, targets=[], prompt="q")  # [] also means the full panel
    assert "voices[1]" in out
    assert "cli: a" in out


async def test_consensus_tool_accepts_a_single_target_string() -> None:
    app = make_app(
        adapters=[FakeAdapter("a"), FakeAdapter("b")],
        runner=FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok")),
    )
    out = await consensus_tool(app, targets="a", prompt="q")  # an explicit single CLI, not "all"
    assert "voices[1]" in out
    assert "cli: a" in out


async def test_job_status_unknown_raises() -> None:
    app = make_app(adapters=[FakeAdapter("fake")])
    with pytest.raises(RutherfordError, match="unknown job"):
        await job_status_tool(app, job_id="nope")
