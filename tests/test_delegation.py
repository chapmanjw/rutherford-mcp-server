# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the delegation service's up-front guards: unknown target and the trusted-workspace check."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

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


# --- direct_workspace_mutation (unsandboxed mutating runs) ------------------------------------------------------


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


def _allowing(tmp_path: Path) -> RutherfordConfig:
    """The only configuration under which a direct mutating run is permitted at all."""
    return RutherfordConfig(allow_direct_workspace_mutation=True, trusted_workspaces=[str(tmp_path)])


def _direct_write(tmp_path: Path | None, **over: object) -> DelegationRequest:
    fields: dict[str, object] = {
        "target": Target(cli="fake"),
        "prompt": "hi",
        "safety_mode": SafetyMode.WRITE,
        "direct_workspace_mutation": True,
    }
    if tmp_path is not None:
        fields["working_dir"] = str(tmp_path)
    fields.update(over)
    return DelegationRequest(**fields)  # type: ignore[arg-type]


async def test_propose_cannot_mutate_the_workspace_directly() -> None:
    result = await _service().delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="hi", safety_mode=SafetyMode.PROPOSE, direct_workspace_mutation=True
        )
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


async def test_direct_mutation_is_refused_unless_the_operator_enabled_it(tmp_path: Path) -> None:
    """The request is otherwise perfect: allowlisted dir, explicit path, top level, write mode.

    This is the case the capability exists to stop -- a caller assembling a valid-looking unsandboxed write
    entirely on its own. The only thing missing is a decision no caller can make for itself.
    """
    service = _service(RutherfordConfig(trusted_workspaces=[str(tmp_path)]))
    called = _spy_runners(service)
    result = await service.delegate(_direct_write(tmp_path))
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT
    assert called == {"direct": False, "sandboxed": False}, "a refused run must not reach either runner"


async def test_direct_mutation_requires_an_explicit_working_dir(tmp_path: Path) -> None:
    """No falling back to the server's own directory, which for a stdio server is the caller's live tree."""
    service = _service(_allowing(tmp_path))
    called = _spy_runners(service)
    result = await service.delegate(_direct_write(None))
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT
    assert called == {"direct": False, "sandboxed": False}


async def test_trust_workspace_alone_cannot_authorize_direct_mutation(tmp_path: Path) -> None:
    """The per-call trust claim opens the ordinary write gate but must not open this one.

    ``trust_workspace`` is set by whoever makes the call, so honouring it here would let the caller grant
    itself the exact permission the operator withheld -- the allowlist would decide nothing.
    """
    service = _service(RutherfordConfig(allow_direct_workspace_mutation=True))  # enabled, but nothing allowlisted
    called = _spy_runners(service)
    result = await service.delegate(_direct_write(tmp_path, trust_workspace=True))
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.WORKSPACE_NOT_TRUSTED
    assert called == {"direct": False, "sandboxed": False}


async def test_direct_mutation_is_refused_inside_a_delegation_chain(tmp_path: Path) -> None:
    """A nested call's prompt was written by a model, not by whoever authorised the outer one.

    This is a PREDICATE test and nothing more: it hands ``base_depth=1`` straight to the service, so it would
    still pass if the tool layer stopped reading the depth at all. The wiring it cannot speak for lives in
    ``tests/test_tools.py`` -- ``test_the_delegate_tool_refuses_direct_mutation_inside_a_nested_rutherford``
    and its unreadable-depth sibling drive the real entrypoint with the environment a nested server inherits.
    Both halves are needed; neither substitutes for the other.
    """
    service = _service(_allowing(tmp_path))
    called = _spy_runners(service)
    result = await service.delegate(_direct_write(tmp_path), base_depth=1)
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT
    assert called == {"direct": False, "sandboxed": False}


async def test_direct_mutation_runs_direct_when_every_condition_is_met(tmp_path: Path) -> None:
    service = _service(_allowing(tmp_path))
    called = _spy_runners(service)
    result = await service.delegate(_direct_write(tmp_path))
    assert result.ok is True
    assert called == {"direct": True, "sandboxed": False}


async def test_direct_mutation_is_a_no_op_for_read_only(tmp_path: Path) -> None:
    """read_only already runs in place and writes nothing, so the flag asks for what it already does."""
    service = _service()  # capability NOT enabled, and the dir is not allowlisted
    called = _spy_runners(service)
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="hi", working_dir=str(tmp_path), direct_workspace_mutation=True
        )
    )
    assert result.ok is True, "a read_only run must not be refused by a gate about writing"
    assert called == {"direct": True, "sandboxed": False}


async def test_a_direct_run_is_audited_and_marked(tmp_path: Path, monkeypatch: Any) -> None:
    """The audit record and the result flag are the ONLY trace: there is no diff and no changed-file list.

    Admission is logged before launch rather than after, so a run that hangs still leaves evidence that an
    agent was turned loose on the tree. The record is best-effort, not guaranteed -- see docs/security.md;
    an operator who needs a trail that survives a kill should collect stderr or enable persistence.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "rutherford.services.delegation.log_event",
        lambda event, **fields: events.append((event, fields)),
    )
    service = _service(_allowing(tmp_path))
    _spy_runners(service)
    result = await service.delegate(_direct_write(tmp_path))

    assert result.direct_mutation is True, "the result must say the diff is missing because nothing captured it"
    assert result.diff is None and result.changed_files is None
    names = [name for name, _ in events]
    assert "direct_workspace_mutation_admitted" in names, f"an unsandboxed run went unrecorded; got {names}"
    assert "direct_workspace_mutation_finished" in names
    admitted = next(fields for name, fields in events if name == "direct_workspace_mutation_admitted")
    assert admitted["working_dir"] == str(tmp_path) and admitted["safety_mode"] == "write"


async def test_a_refused_direct_run_is_not_audited_as_admitted(tmp_path: Path, monkeypatch: Any) -> None:
    """An admission record for a run that never started would make the audit trail lie about what happened."""
    events: list[str] = []
    monkeypatch.setattr(
        "rutherford.services.delegation.log_event",
        lambda event, **fields: events.append(event),
    )
    service = _service(RutherfordConfig(trusted_workspaces=[str(tmp_path)]))  # capability disabled
    await service.delegate(_direct_write(tmp_path))
    assert "direct_workspace_mutation_admitted" not in events


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


# ``C:foo`` and ``~`` are the ones worth spelling out: the first LOOKS absolute but is drive-relative on
# Windows and resolves against the cwd, and Path never expands the second, so it names a literal "~"
# directory under the cwd. Both are non-absolute on POSIX too, so the case stays portable.
@pytest.mark.parametrize("relative", [".", "sub", "./sub", "..", "C:foo", "~"])
async def test_direct_mutation_refuses_a_relative_working_dir(tmp_path: Path, monkeypatch: Any, relative: str) -> None:
    """A relative path resolves against the server's own directory -- the exact thing the gate refuses.

    ``working_dir="."`` satisfies "a working_dir was supplied" while naming nothing, and would have been
    resolved against the process cwd: for a stdio MCP server, the caller's live tree. It also makes the audit
    record useless, since '.' identifies no directory after the fact.
    """
    monkeypatch.chdir(tmp_path)  # so a relative path would resolve INTO the allowlisted root if permitted
    service = _service(_allowing(tmp_path))
    called = _spy_runners(service)
    result = await service.delegate(_direct_write(None, working_dir=relative))
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.INVALID_INPUT
    assert called == {"direct": False, "sandboxed": False}


async def test_direct_mutation_runs_against_the_path_it_authorised(tmp_path: Path) -> None:
    """The run must use the resolved directory the gate approved, not the string the caller sent.

    The allowlist check has to resolve the path to compare it. If the launch then used the original string,
    approval and use would be two separate lookups of the same name, with a window in between for it to mean
    something else.
    """
    root = tmp_path / "root"
    root.mkdir()
    link_parent = tmp_path / "via"
    link_parent.mkdir()
    seen: list[str | None] = []

    async def _direct(req: DelegationRequest, *a: object, **kw: object) -> DelegationResult:
        seen.append(req.working_dir)
        return DelegationResult(target=req.target, ok=True)

    service = _service(_allowing(root))
    service._run_direct = _direct  # type: ignore[method-assign]
    # An indirect but absolute spelling of the same directory: resolve() collapses it, the allowlist matches,
    # and the launch must receive the collapsed form.
    indirect = str(link_parent / ".." / "root")
    result = await service.delegate(_direct_write(None, working_dir=indirect))
    assert result.ok is True
    assert seen == [str(root.resolve())], f"the run launched against {seen} rather than the approved path"


async def test_the_direct_mutation_audit_outranks_the_configured_log_level(tmp_path: Path, monkeypatch: Any) -> None:
    """The audit has to survive a log level chosen for unrelated reasons.

    ``log_format = "off"`` is refused at config load, but ``log_level = "error"`` is a perfectly ordinary
    setting that silently discards WARNING -- which is where these records used to be emitted. That would
    have rebuilt the same no-trace configuration the validator exists to prevent, by a different route.
    Asserting the LEVEL rather than the text, because that is the property that keeps the record.
    """
    levels: list[int] = []
    monkeypatch.setattr(
        "rutherford.services.delegation.log_event",
        lambda event, **fields: (
            levels.append(int(fields.get("level", logging.INFO)))
            if event.startswith("direct_workspace_mutation")
            else None
        ),
    )
    service = _service(_allowing(tmp_path))
    _spy_runners(service)
    result = await service.delegate(_direct_write(tmp_path))
    assert result.ok is True
    assert levels, "the run emitted no audit record at all"
    assert all(level >= logging.ERROR for level in levels), (
        f"an audit record was emitted below ERROR ({levels}), so log_level='error' would discard it"
    )
