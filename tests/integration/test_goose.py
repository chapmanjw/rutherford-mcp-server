# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration test: drive the real ``goose acp`` agent over ACP (local only, -m integration)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rutherford.acp.descriptors import default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.domain.enums import SafetyMode

pytestmark = pytest.mark.integration


async def test_goose_real_turn() -> None:
    goose = default_registry().get("goose")
    result = await run_acp_turn(
        goose,
        "Reply with ONLY the number, nothing else: what is 17 + 25?",
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=str(Path.cwd()),
        timeout_s=120.0,
    )
    assert result.ok is True, f"goose failed: {result.error}"
    assert "42" in result.text
    assert result.session_id is not None
