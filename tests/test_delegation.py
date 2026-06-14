# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation service's up-front guards: unknown target and the trusted-workspace check."""

from __future__ import annotations

from pathlib import Path

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationRequest, Target
from rutherford.services.delegation import DelegationService

_FAKE = AgentDescriptor("fake", "Fake", ("x",))


def _service(config: RutherfordConfig | None = None) -> DelegationService:
    return DelegationService(DescriptorRegistry([_FAKE]), config or RutherfordConfig())


async def test_unknown_target_returns_a_failed_result_not_raised() -> None:
    result = await _service().delegate(DelegationRequest(target=Target(cli="nope"), prompt="hi"))
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.UNKNOWN_TARGET


async def test_write_mode_without_trust_is_refused(tmp_path: Path) -> None:
    result = await _service().delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="hi", safety_mode=SafetyMode.WRITE, working_dir=str(tmp_path)
        )
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.WORKSPACE_NOT_TRUSTED


def test_workspace_trusted_variants(tmp_path: Path) -> None:
    service = _service(RutherfordConfig(trusted_workspaces=[str(tmp_path)]))
    # an explicit trust_workspace wins regardless of the configured allowlist
    assert service._workspace_trusted(DelegationRequest(target=Target(cli="fake"), prompt="p", trust_workspace=True))
    # no working_dir -> not trusted
    assert not service._workspace_trusted(DelegationRequest(target=Target(cli="fake"), prompt="p"))
    # a dir under a trusted root -> trusted
    sub = tmp_path / "sub"
    sub.mkdir()
    assert service._workspace_trusted(DelegationRequest(target=Target(cli="fake"), prompt="p", working_dir=str(sub)))
    # a dir outside every trusted root -> not trusted
    assert not service._workspace_trusted(
        DelegationRequest(target=Target(cli="fake"), prompt="p", working_dir=str(tmp_path.parent))
    )
