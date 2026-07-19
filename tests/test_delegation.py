# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation service's up-front guards: unknown target and the trusted-workspace check."""

from __future__ import annotations

from pathlib import Path

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationRequest, DelegationResult, Target
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


# --- sandbox=False (unsandboxed mutating runs) ------------------------------------------------------


def _spy_runners(service: DelegationService) -> dict[str, bool]:
    """Replace both runners with stubs that record which path was taken."""
    called = {"direct": False, "sandboxed": False}

    async def _direct(req: DelegationRequest, *a: object, **kw: object) -> DelegationResult:
        called["direct"] = True
        return DelegationResult(target=req.target, ok=True)

    async def _sandboxed(req: DelegationRequest, *a: object, **kw: object) -> DelegationResult:
        called["sandboxed"] = True
        return DelegationResult(target=req.target, ok=True)

    service._run_direct = _direct  # type: ignore[method-assign]
    service._run_sandboxed = _sandboxed  # type: ignore[method-assign]
    return called


async def test_propose_without_sandbox_is_refused() -> None:
    result = await _service().delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", safety_mode=SafetyMode.PROPOSE, sandbox=False)
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT


async def test_sandboxed_write_without_working_dir_is_refused(tmp_path: Path) -> None:
    # even a trusted workspace does not save a sandboxed mutating run with no tree to isolate
    service = _service(RutherfordConfig(trusted_workspaces=[str(tmp_path)]))
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", safety_mode=SafetyMode.WRITE, trust_workspace=True)
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT


async def test_unsandboxed_write_without_working_dir_pins_cwd_into_trust_gate() -> None:
    # sandbox=False with no working_dir inherits the server cwd, which the default config does not trust
    result = await _service().delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", safety_mode=SafetyMode.WRITE, sandbox=False)
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.WORKSPACE_NOT_TRUSTED


async def test_unsandboxed_write_in_trusted_workspace_runs_direct(tmp_path: Path) -> None:
    service = _service(RutherfordConfig(trusted_workspaces=[str(tmp_path)]))
    called = _spy_runners(service)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="hi",
            safety_mode=SafetyMode.WRITE,
            sandbox=False,
            working_dir=str(tmp_path),
        )
    )
    assert result.ok is True
    assert called == {"direct": True, "sandboxed": False}


async def test_sandboxed_write_in_trusted_workspace_runs_sandboxed(tmp_path: Path) -> None:
    service = _service(RutherfordConfig(trusted_workspaces=[str(tmp_path)]))
    called = _spy_runners(service)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="hi", safety_mode=SafetyMode.WRITE, working_dir=str(tmp_path)
        )
    )
    assert result.ok is True
    assert called == {"direct": False, "sandboxed": True}


async def test_read_only_always_runs_direct(tmp_path: Path) -> None:
    service = _service()
    called = _spy_runners(service)
    result = await service.delegate(
        DelegationRequest(target=Target(cli="fake"), prompt="hi", working_dir=str(tmp_path))
    )
    assert result.ok is True
    assert called == {"direct": True, "sandboxed": False}
