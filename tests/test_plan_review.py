# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the read-only role-driven tools: ``plan`` (architect delegate) and ``review`` (reviewer consensus).

Both are thin wrappers that clamp safety to ``read_only`` and prepend a built-in role -- ``plan`` the
``architect`` persona over one agent, ``review`` the ``principal-reviewer`` persona over a panel. These drive
the fake ACP agent end to end (which echoes the prompt, so the role preamble is observable) and assert the
read-only clamp, the persona, and the panel/diff handling.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationResult
from rutherford.io.serialize import decode
from rutherford.tools.plan import plan_tool
from rutherford.tools.review import review_tool

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

#: A tiny three-line patch, the unit a live review acts on.
_DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1 +1 @@
-def add(a, b): return a - b
+def add(a, b): return a + b
"""


def _app() -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([FAKE, FAKE_A]))


# --- plan --------------------------------------------------------------------


async def test_plan_runs_an_architect_read_only_delegate() -> None:
    out = await plan_tool(_app(), cli="fake", goal="add a cache layer", working_dir=str(REPO_ROOT))
    result = DelegationResult.model_validate(decode(out))
    assert result.ok is True
    assert result.safety_mode.value == "read_only"  # planning is clamped to read-only
    # The fake echoes the first 40 chars of the composed prompt, which begins with the architect persona body,
    # so the echo proves the architect preamble led the prompt the agent received.
    assert "architect" in result.text.lower()


async def test_plan_unknown_agent_raises_before_running() -> None:
    with pytest.raises(RutherfordError) as exc:
        await plan_tool(_app(), cli="ghost", goal="x", working_dir=str(REPO_ROOT))
    assert exc.value.code is ErrorCode.UNKNOWN_TARGET


async def test_plan_takes_no_safety_mode() -> None:
    # The signature has no safety_mode parameter, so a plan can never be asked to mutate.
    import inspect

    assert "safety_mode" not in inspect.signature(plan_tool).parameters


# --- review ------------------------------------------------------------------


async def test_review_over_a_diff_runs_a_reviewer_consensus() -> None:
    out = await review_tool(_app(), targets=["fake", "fake_a"], diff=_DIFF, working_dir=str(REPO_ROOT))
    # The all-voices consensus envelope is a quoted-array TOON (a known python-toon round-trip quirk), so assert
    # on the encoded string. Both panel seats reviewed under the principal-reviewer persona.
    assert "safety_mode: read_only" in out  # the review voices ran read-only
    assert out.lower().count("reviewing code") == 2  # both voices got the principal-reviewer preamble
    assert "cli: fake\n" in out and "fake_a" in out


async def test_review_requires_diff_or_paths() -> None:
    with pytest.raises(RutherfordError) as exc:
        await review_tool(_app(), targets=["fake"], working_dir=str(REPO_ROOT))
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_review_synthesizes_by_default_and_honors_an_explicit_false() -> None:
    # synthesize defaults on for review (unlike consensus): a single voice synthesizes from itself, so the
    # envelope carries a synthesis with no explicit flag; an explicit ``synthesize=false`` suppresses it.
    on = await review_tool(_app(), targets=["fake"], diff=_DIFF, working_dir=str(REPO_ROOT))
    assert "synthesis" in on
    off = await review_tool(_app(), targets=["fake"], diff=_DIFF, synthesize=False, working_dir=str(REPO_ROOT))
    assert "synthesis:" not in off  # an explicit false suppresses the combined answer


async def test_review_unknown_agent_raises() -> None:
    with pytest.raises(RutherfordError) as exc:
        await review_tool(_app(), targets=["ghost"], diff=_DIFF, working_dir=str(REPO_ROOT))
    assert exc.value.code is ErrorCode.UNKNOWN_TARGET
