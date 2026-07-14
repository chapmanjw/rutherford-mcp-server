# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for driving a turn (run_acp_turn) and the delegation service, against the fake ACP agent."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.journal import EventJournal, JournalEvent
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPHandshakeError, ACPSession, _post_prompt_safety, run_acp_turn
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import ReexecutionSafety, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.models import DelegationRequest, DelegationResult, Target
from rutherford.services.delegation import DelegationService

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_ACP_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", FAKE_ACP_CMD)
_READ_ONLY = PermissionPolicy(SafetyMode.READ_ONLY)


async def _turn(prompt: str, *, timeout_s: float = 60.0, descriptor: AgentDescriptor = FAKE) -> DelegationResult:
    return await run_acp_turn(descriptor, prompt, policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=timeout_s)


def test_handshake_timeout_override() -> None:
    # The descriptor's budget is used by default; a connection probe overrides it so a generous local-model
    # floor reaches each handshake step (the value used in every wait_for inside open()).
    desc = AgentDescriptor("x", "X", ("x",), handshake_timeout_s=30.0)
    assert ACPSession(desc, policy=_READ_ONLY, cwd=".")._handshake_timeout == 30.0
    assert ACPSession(desc, policy=_READ_ONLY, cwd=".", handshake_timeout_s=99.0)._handshake_timeout == 99.0


async def test_run_turn_normal_answer() -> None:
    result = await _turn("what is 17 + 25?")
    assert result.ok is True and "42" in result.text
    assert result.session_id == "fake-session-1"
    assert result.provenance is not None
    assert result.safety_mode is SafetyMode.READ_ONLY


async def test_run_turn_refusal() -> None:
    result = await _turn("REFUSE this request")
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.ACP_REFUSED
    assert result.error.reexecution_safety is ReexecutionSafety.DUPLICATE_COST


# --- session resume (ACP session/load) ---------------------------------------


async def test_run_turn_resumes_a_session_via_load() -> None:
    """A resume drives ACP ``session/load`` instead of ``session/new``: the turn runs under the REQUESTED id
    (not the fake's fixed ``fake-session-1`` from new_session), and the agent confirms it reloaded it."""
    result = await run_acp_turn(
        FAKE, "WHOAMI", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, resume_session_id="resume-me-123"
    )
    assert result.ok is True
    assert result.session_id == "resume-me-123"  # the loaded id, not new_session's "fake-session-1"
    assert "resumed=yes" in result.text


async def test_run_turn_without_resume_is_a_fresh_session() -> None:
    """The default path (no resume) creates a fresh session/new, so the agent reports it was NOT resumed."""
    result = await run_acp_turn(FAKE, "WHOAMI", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert result.ok is True
    assert result.session_id == "fake-session-1"
    assert "resumed=no" in result.text


async def test_run_turn_resume_unsupported_agent_is_resume_failed() -> None:
    """An agent that does not advertise the loadSession capability cannot resume: a clean RESUME_FAILED."""
    noload = AgentDescriptor("noload", "NoLoad", FAKE.command, env_overrides=(("RUTHERFORD_FAKE_NO_LOADSESSION", "1"),))
    result = await run_acp_turn(
        noload, "WHOAMI", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, resume_session_id="nope"
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.RESUME_FAILED
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE  # pre-prompt, no side effect


async def test_delegate_threads_session_id_into_a_resume() -> None:
    """The delegate flow wires ``DelegationRequest.session_id`` through to the ACP resume path."""
    service = DelegationService(DescriptorRegistry([FAKE]), RutherfordConfig())
    result = await service.delegate(
        DelegationRequest(
            target=Target(cli="fake"),
            prompt="WHOAMI",
            working_dir=str(REPO_ROOT),
            session_id="carryover-9",
            timeout_s=60.0,
        )
    )
    assert result.ok is True
    assert result.session_id == "carryover-9" and "resumed=yes" in result.text


async def test_run_turn_empty_answer() -> None:
    result = await _turn("EMPTY answer please")
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_EMPTY_ANSWER


async def test_run_turn_timeout() -> None:
    result = await _turn("HANG forever", timeout_s=1.0)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_TURN_TIMEOUT
    assert result.error.reexecution_safety is ReexecutionSafety.DUPLICATE_COST


async def test_run_turn_spawn_failure() -> None:
    bad = AgentDescriptor("bad", "Bad", ("this-binary-does-not-exist-xyz123",))
    result = await _turn("hi", descriptor=bad)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_SPAWN_FAILED
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE


def _service() -> DelegationService:
    return DelegationService(DescriptorRegistry([FAKE]), RutherfordConfig())


async def test_delegation_ok_with_files() -> None:
    request = DelegationRequest(
        target=Target(cli="fake"), prompt="what is 17 + 25?", working_dir=str(REPO_ROOT), files=["a.py", "b.py"]
    )
    result = await _service().delegate(request)
    assert result.ok is True and "42" in result.text


async def test_delegation_unknown_agent() -> None:
    result = await _service().delegate(DelegationRequest(target=Target(cli="nope"), prompt="x"))
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.UNKNOWN_TARGET


async def test_delegation_untrusted_write_is_refused() -> None:
    request = DelegationRequest(
        target=Target(cli="fake"), prompt="x", safety_mode=SafetyMode.WRITE, working_dir=str(REPO_ROOT)
    )
    result = await _service().delegate(request)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.WORKSPACE_NOT_TRUSTED


async def test_delegation_trusted_write_runs() -> None:
    request = DelegationRequest(
        target=Target(cli="fake"),
        prompt="what is 17 + 25?",
        safety_mode=SafetyMode.WRITE,
        working_dir=str(REPO_ROOT),
        trust_workspace=True,
    )
    result = await _service().delegate(request)
    assert result.ok is True


def test_post_prompt_safety_classification() -> None:
    side = EventJournal()
    side.append(JournalEvent(kind="fs_write", detail="x"))
    assert _post_prompt_safety(side) is ReexecutionSafety.SIDE_EFFECTED
    tool = EventJournal()
    tool.append(JournalEvent(kind="tool_call", tool_call_id="t"))
    assert _post_prompt_safety(tool) is ReexecutionSafety.AMBIGUOUS
    assert _post_prompt_safety(EventJournal()) is ReexecutionSafety.DUPLICATE_COST


async def test_run_turn_records_requested_and_selected_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # set_model-only: advertise the id, select it, and stamp requested/selected/confirmed on the envelope.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "fake-model")
    result = await run_acp_turn(
        FAKE, "what is 17 + 25?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="fake-model"
    )
    assert result.ok is True
    assert result.target.model == "fake-model"
    assert result.requested_model == "fake-model"
    assert result.selected_model == "fake-model"
    assert result.provenance is not None
    assert result.provenance.model == "fake-model"
    assert result.provenance.confirmed is True


async def test_run_turn_without_model_keeps_default_path() -> None:
    # model=None and no descriptor default: no selection, no confirmed provenance model.
    result = await run_acp_turn(FAKE, "what is 17 + 25?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert result.ok is True
    assert result.target.model is None
    assert result.requested_model is None
    assert result.selected_model is None
    assert result.provenance is not None
    assert result.provenance.model is None
    assert result.provenance.confirmed is False


async def test_descriptor_default_on_channel_less_agent_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    # Descriptor default with no ACP model channels: turn runs on the agent default without claiming selection.
    monkeypatch.delenv("RUTHERFORD_FAKE_MODELS", raising=False)
    monkeypatch.delenv("RUTHERFORD_FAKE_MODEL_OPTION", raising=False)
    desc = AgentDescriptor("fake", "Fake", FAKE.command, default_model="m1")
    result = await run_acp_turn(desc, "what is 17 + 25?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert result.ok is True
    assert result.requested_model == "m1"
    assert result.target.model == "m1"
    assert result.selected_model is None  # never in-session confirmed
    assert result.provenance is not None
    assert result.provenance.model == "m1"  # effective model that ran (for F3 lineage); confirmed carries attestation
    assert result.provenance.confirmed is False


async def test_descriptor_default_unadvertised_on_config_agent_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression (Bedrock/Vertex seat): a config-ADVERTISING agent (claude_code advertises alias options) whose
    # descriptor default_model is NOT among the advertised values. The shipped Bedrock remediation sets
    # default_model to a provider inference-profile id that is applied via injected ANTHROPIC_MODEL, never on an
    # ACP channel. With no explicit caller model and no effort rewrite this must SOFT-SKIP (the model is applied
    # out-of-band), not hard-fail MODEL_UNAVAILABLE. Before the fix, has_channels=True forced a raise and broke
    # every turn of the seat -- the older soft test used a channel-LESS fake, which claude_code is not.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet,haiku")  # the agent DOES advertise a channel
    provider_id = "global.anthropic.claude-opus-4-8[1m]"
    desc = AgentDescriptor("bedrockish", "Bedrockish", FAKE.command, default_model=provider_id)
    result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert result.ok is True  # soft-skip, not MODEL_UNAVAILABLE
    assert result.requested_model == provider_id
    assert result.target.model == provider_id
    assert result.selected_model is None  # never selected over ACP
    assert "set_config_calls=0" in result.text  # no in-session selection attempted for the out-of-band default
    assert result.provenance is not None
    assert result.provenance.model == provider_id  # effective model kept for F3 lineage
    assert result.provenance.confirmed is False


# --- model selection across the two ACP channels -----------------------------


async def test_select_model_via_config_option_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    # claude_code's claude-agent-acp advertises its models on the configOptions "model" channel, NOT in
    # SessionModelState. A requested model the option advertises is set via session/set_config_option, and the
    # agent echoes it back -- proof the SECOND channel reached the agent (without this, no model is selectable
    # for claude_code at all).
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet,haiku")
    result = await run_acp_turn(FAKE, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is True
    assert "model=sonnet" in result.text
    assert result.selected_model == "sonnet"
    assert result.provenance is not None and result.provenance.confirmed is True


async def test_unadvertised_model_is_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A model advertised on NEITHER channel fails hard (MODEL_UNAVAILABLE) instead of silently running on the
    # agent default -- an explicit request must not be reported as selected when it was never applied.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet,haiku")
    result = await run_acp_turn(
        FAKE, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="claude-opus-4-8"
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.MODEL_UNAVAILABLE
    assert result.selected_model is None
    assert result.requested_model == "claude-opus-4-8"


async def test_open_tears_down_agent_on_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: _select_model raises MODEL_UNAVAILABLE AFTER the agent process is spawned. open() is entered
    # via ``async with`` (run_acp_turn), so Python skips ``__aexit__`` when open() raises -- open() must tear the
    # agent down itself on that raise, or the spawned process leaks (the same leak class an unguarded channel-1
    # read caused). Removing the try/except guarding _select_model / _select_effort in open() fails this test.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet")
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT), model="claude-opus-4-8")
    with pytest.raises(ACPHandshakeError) as exc:
        await session.open()
    assert exc.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert session._pid is None  # close() ran on the failure path -> the spawned agent was reaped, not leaked


async def test_dual_channel_prefers_verified_config_option(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dual-channel without model_launch_flag: both session.models and a model config option advertise the id.
    # set_model is a no-op for the real channel; Rutherford must use set_config_option (and not call set_model).
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "sonnet,default")
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet")
    result = await run_acp_turn(FAKE, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is True
    assert "model=sonnet" in result.text
    assert "set_model_calls=0" in result.text
    assert "set_config_calls=1" in result.text
    assert result.selected_model == "sonnet"
    assert result.provenance is not None and result.provenance.confirmed is True


async def test_already_current_config_model_skips_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    # When current_value already equals the target, skip set_config_option and still confirm (in-session path).
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "sonnet,haiku")
    result = await run_acp_turn(FAKE, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is True
    assert "model=(unset)" in result.text  # no set_config_option call, so fake never recorded a set
    assert "set_config_calls=0" in result.text
    assert result.selected_model == "sonnet"
    assert result.provenance is not None and result.provenance.confirmed is True


async def test_config_option_current_value_mismatch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet")
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_MISMATCH", "1")
    result = await run_acp_turn(FAKE, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.MODEL_UNAVAILABLE
    assert "not confirmed" in (result.error.message or "")
    assert result.selected_model is None


async def test_set_model_only_channel_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "gpt-5.2,gpt-4")
    result = await run_acp_turn(
        FAKE, "what is 17 + 25?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="gpt-5.2"
    )
    assert result.ok is True
    assert result.selected_model == "gpt-5.2"
    assert result.provenance is not None and result.provenance.confirmed is True


async def test_legacy_set_model_unavailable_when_sdk_lacks_method(monkeypatch: pytest.MonkeyPatch) -> None:
    # ACP 0.11+ drops ClientSideConnection.set_session_model. A legacy-only advertisement must fail as
    # MODEL_UNAVAILABLE with a clear detail -- never AttributeError / INTERNAL. On 0.10.x the method is
    # removed only when present; on 0.11+ it is already absent (no unconditional delattr).
    from types import SimpleNamespace

    from acp.client.connection import ClientSideConnection

    if hasattr(ClientSideConnection, "set_session_model"):
        monkeypatch.delattr(ClientSideConnection, "set_session_model")
    # * Inject a duck-typed legacy SessionModelState so the path is exercised even when the fake/SDK cannot
    # emit typed session.models (ACP 0.11+). No config option -- set_model-only branch.
    legacy_models = SimpleNamespace(
        available_models=[SimpleNamespace(model_id="gpt-5.2"), SimpleNamespace(model_id="gpt-4")]
    )
    legacy_only = SimpleNamespace(session_id="legacy-only-1", config_options=None, models=legacy_models)

    async def _fake_new(self: ACPSession, conn: object) -> object:
        self._session_id = "legacy-only-1"
        return legacy_only

    monkeypatch.setattr(ACPSession, "_new_session", _fake_new)
    result = await run_acp_turn(
        FAKE, "what is 17 + 25?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="gpt-5.2"
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.MODEL_UNAVAILABLE
    assert "set_session_model" in (result.error.message or "")
    assert result.selected_model is None
    assert result.requested_model == "gpt-5.2"


# --- ACP 0.11 config-only responses (no session.models attribute) -------------


def _config_only_session(*, values: list[str], current: str | None = None, session_id: str = "cfg-only-1") -> object:
    """A NewSessionResponse-shaped object with configOptions model select and NO ``.models`` attribute."""
    from types import SimpleNamespace

    from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption

    current_value = current if current is not None else values[0]
    select = SessionConfigOptionSelect(
        id="model",
        name="Model",
        type="select",
        current_value=current_value,
        options=[SessionConfigSelectOption(name=value, value=value) for value in values],
        category="model",
    )
    return SimpleNamespace(session_id=session_id, config_options=[select])


def test_models_of_and_advertises_tolerate_missing_models_attr() -> None:
    # ACP 0.11 response shape: attribute absent entirely (not models=None).
    from types import SimpleNamespace

    from rutherford.acp.session import _advertises_model, _models_of

    session = SimpleNamespace(session_id="s1", config_options=[])
    assert not hasattr(session, "models")
    assert _models_of(session) == []
    assert _advertises_model(session, "sonnet") is False


def test_models_of_extracts_legacy_typed_state() -> None:
    # The legacy channel-1 shape (session.models -> available_models -> model_id) via a duck-typed stand-in:
    # SessionModelState / ModelInfo were removed in ACP 0.11+, and _models_of reads the channel purely through
    # getattr, so a SimpleNamespace exercises the exact extraction path a real 0.10.x SessionModelState would.
    from types import SimpleNamespace

    from rutherford.acp.session import _advertises_model, _models_of

    state = SimpleNamespace(
        available_models=[SimpleNamespace(model_id="gpt-5.2", name="GPT")],
        current_model_id="gpt-5.2",
    )
    session = SimpleNamespace(models=state)
    assert _models_of(session) == ["gpt-5.2"]
    assert _advertises_model(session, "gpt-5.2") is True
    assert _advertises_model(session, "other") is False


async def test_open_and_available_models_with_config_only_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # open() must not AttributeError when session/new returns a config-only shape; available_models is the union.
    config_only = _config_only_session(values=["default", "sonnet", "haiku"], current="default")

    async def _fake_new(self: ACPSession, conn: object) -> object:
        self._session_id = "cfg-only-1"
        return config_only

    monkeypatch.setattr(ACPSession, "_new_session", _fake_new)
    session = ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    await session.open()
    try:
        assert session.available_models == ["default", "sonnet", "haiku"]
        assert session.session_id == "cfg-only-1"
    finally:
        await session.close()


async def test_launch_validate_passes_config_only_advertisement(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cursor launch path: validate via config option when legacy .models is absent; stay unconfirmed.
    config_only = _config_only_session(values=["sonnet", "haiku"], current="sonnet")

    async def _fake_new(self: ACPSession, conn: object) -> object:
        self._session_id = "cfg-only-1"
        return config_only

    monkeypatch.setattr(ACPSession, "_new_session", _fake_new)
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is True
    assert result.argv is not None and result.argv[-2:] == ["--model", "sonnet"]
    assert result.requested_model == "sonnet"
    assert result.target.model == "sonnet"
    assert result.selected_model is None
    assert result.provenance is not None and result.provenance.confirmed is False


async def test_launch_unadvertised_model_config_only_proceeds_via_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    # Launch-flag agent on a 0.11 config-only response that does NOT advertise the requested model. The id is
    # applied via the --model argv regardless, so the turn proceeds unconfirmed rather than hard-failing:
    # launch routing must never be blocked on an ACP advertisement the agent may not carry on 0.11.
    config_only = _config_only_session(values=["default", "sonnet"], current="default")

    async def _fake_new(self: ACPSession, conn: object) -> object:
        self._session_id = "cfg-only-1"
        return config_only

    monkeypatch.setattr(ACPSession, "_new_session", _fake_new)
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    result = await run_acp_turn(
        desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="claude-opus-4-8"
    )
    assert result.ok is True
    assert result.selected_model is None  # applied via argv, never in-session confirmed
    assert result.requested_model == "claude-opus-4-8"
    assert result.argv is not None and result.argv[-2:] == ["--model", "claude-opus-4-8"]


async def test_in_session_select_via_config_only_no_models_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    # In-session path on a 0.11-shaped response: set_config_option confirms; no legacy channel required.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet")
    config_only = _config_only_session(values=["default", "sonnet"], current="default")

    async def _fake_new(self: ACPSession, conn: object) -> object:
        self._session_id = "cfg-only-1"
        return config_only

    monkeypatch.setattr(ACPSession, "_new_session", _fake_new)
    result = await run_acp_turn(FAKE, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is True
    assert result.selected_model == "sonnet"
    assert result.provenance is not None and result.provenance.confirmed is True
    assert "model=sonnet" in result.text
    assert "set_config_calls=1" in result.text


# --- launch-flag model selection (Cursor-style model_launch_flag) -------------


def test_model_launch_flag_appends_effective_model_without_mutating_command() -> None:
    # Effective model (post-effort) is layered onto a fresh argv; the descriptor command tuple stays immutable.
    from rutherford.domain.enums import Effort

    command = FAKE_ACP_CMD
    desc = AgentDescriptor("cursorish", "Cursorish", command, model_launch_flag="--model", effort_base="cursor")
    session = ACPSession(desc, policy=_READ_ONLY, cwd=str(REPO_ROOT), model="gpt-5.2", effort=Effort.HIGH)
    assert desc.command == command
    assert session.launch_argv == [*command, "--model", "gpt-5.2-high"]
    assert session.target.model == "gpt-5.2-high"
    assert session.requested_model == "gpt-5.2"


def test_model_launch_flag_omitted_when_no_model() -> None:
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    session = ACPSession(desc, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    assert session.launch_argv == list(FAKE_ACP_CMD)
    assert session.target.model is None


async def test_launch_model_skips_in_session_rpc_and_stays_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cursor-like: config option already current (and session.models also advertise). Launch path must NOT
    # call set_config_option / set_model, must NOT treat the echo as confirmed selected_model.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODELS", "sonnet,default")
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "sonnet,default")
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="sonnet")
    assert result.ok is True
    assert "--model" in (result.argv or [])
    assert result.argv is not None and result.argv[result.argv.index("--model") + 1] == "sonnet"
    assert "launch_model=sonnet" in result.text
    assert "model=(unset)" in result.text
    assert "set_model_calls=0" in result.text
    assert "set_config_calls=0" in result.text
    assert result.requested_model == "sonnet"
    assert result.target.model == "sonnet"
    assert result.selected_model is None
    assert result.provenance is not None
    assert result.provenance.model == "sonnet"  # effective model that ran (for F3 lineage); not in-session confirmed
    assert result.provenance.confirmed is False


def test_split_csv_respecting_brackets() -> None:
    from tests.fake_acp_agent import _split_csv_respecting_brackets

    assert _split_csv_respecting_brackets("sonnet,default,haiku") == ["sonnet", "default", "haiku"]
    assert _split_csv_respecting_brackets("grok-4.5[effort=high,fast=true]") == ["grok-4.5[effort=high,fast=true]"]
    assert _split_csv_respecting_brackets("composer-2.5[fast=true],grok-4.5[effort=high,fast=true]") == [
        "composer-2.5[fast=true]",
        "grok-4.5[effort=high,fast=true]",
    ]
    assert _split_csv_respecting_brackets("  a , b  ") == ["a", "b"]
    assert _split_csv_respecting_brackets("") == []
    # * Fail-safe unbalanced: commas stay inside an open bracket; stray ']' does not raise.
    assert _split_csv_respecting_brackets("x[a,b") == ["x[a,b"]
    assert _split_csv_respecting_brackets("a],b") == ["a]", "b"]
    assert _split_csv_respecting_brackets("x[[a,b],c]") == ["x[[a,b],c]"]


def test_cursor_runtime_family_prefix_matching() -> None:
    from tests.integration.test_cursor_model_routing import _runtime_matches_family

    assert _runtime_matches_family(["cursor-grok-4.5-high-fast"], "grok")
    assert _runtime_matches_family(["grok-4.5-high"], "grok")
    assert _runtime_matches_family(["Composer-2.5-fast"], "composer")
    assert not _runtime_matches_family(["not-a-grok-model"], "grok")
    assert not _runtime_matches_family(["my-composer-fork"], "composer")
    assert not _runtime_matches_family(["cursor-grok-4.5"], "composer")
    assert not _runtime_matches_family(["composer-2.5"], "grok")
    assert not _runtime_matches_family(["composer-2.5"], "unknown")
    assert not _runtime_matches_family([], "grok")


async def test_launch_model_accepts_boolean_fast_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    # Live Cursor advertises fast=true while callers may request exact fast=false on --model; launch-only
    # validation must accept that boolean mismatch without rewriting argv or claiming confirmation.
    monkeypatch.setenv(
        "RUTHERFORD_FAKE_MODEL_OPTION",
        "composer-2.5[fast=true],grok-4.5[effort=high,fast=true]",
    )
    command = FAKE_ACP_CMD
    desc = AgentDescriptor("cursorish", "Cursorish", command, model_launch_flag="--model")
    requested = "composer-2.5[fast=false]"
    result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model=requested)
    assert result.ok is True
    assert desc.command == command
    assert result.argv is not None and result.argv[-2:] == ["--model", requested]
    assert "launch_model=" + requested in result.text
    assert "set_model_calls=0" in result.text
    assert "set_config_calls=0" in result.text
    assert result.requested_model == requested
    assert result.target.model == requested
    assert result.selected_model is None
    assert result.provenance is not None and result.provenance.confirmed is False


async def test_launch_model_accepts_grok_fast_variant_same_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "grok-4.5[effort=high,fast=true]")
    requested = "grok-4.5[effort=high,fast=false]"
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model=requested)
    assert result.ok is True
    assert result.argv is not None and result.argv[-2:] == ["--model", requested]
    assert result.selected_model is None
    assert result.provenance is not None and result.provenance.confirmed is False


async def test_launch_model_unadvertised_variants_proceed_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    # None of these launch ids match the advertised config values (differing effort, unknown base, extra/dup
    # params). On a launch-flag agent the id is applied via argv regardless, so each proceeds unconfirmed
    # rather than hard-failing -- launch routing is never blocked on an ACP advertisement 0.11 may not carry.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "grok-4.5[effort=high,fast=true],composer-2.5[fast=true]")
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    for model in (
        "grok-4.5[effort=low,fast=false]",
        "unknown-model[fast=false]",
        "composer-2.5[fast=false,extra=1]",
        "composer-2.5[fast=false,fast=true]",
    ):
        result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model=model)
        assert result.ok is True, model
        assert result.selected_model is None, model  # unconfirmed; applied via --model argv
        assert result.argv is not None and result.argv[-2:] == ["--model", model], model


async def test_in_session_select_model_still_requires_exact_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # * Do not weaken in-session validation: fast=false vs advertised fast=true stays MODEL_UNAVAILABLE.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "composer-2.5[fast=true]")
    desc = AgentDescriptor("plain", "Plain", FAKE_ACP_CMD)  # no model_launch_flag
    result = await run_acp_turn(
        desc,
        "MODEL?",
        policy=_READ_ONLY,
        cwd=str(REPO_ROOT),
        timeout_s=60.0,
        model="composer-2.5[fast=false]",
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.MODEL_UNAVAILABLE


async def test_launch_model_unadvertised_proceeds_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A launch-flag model absent from the advertised config channel proceeds unconfirmed (applied via the
    # --model argv), NOT a hard MODEL_UNAVAILABLE -- a pre-emptive block would break Cursor on acp 0.11, where
    # the legacy channel is gone and the agent may advertise nothing Rutherford can read.
    monkeypatch.setenv("RUTHERFORD_FAKE_MODEL_OPTION", "default,sonnet")
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    result = await run_acp_turn(
        desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0, model="claude-opus-4-8"
    )
    assert result.ok is True
    assert result.selected_model is None
    assert result.requested_model == "claude-opus-4-8"
    assert result.provenance is not None and result.provenance.model == "claude-opus-4-8"  # effective, unconfirmed
    assert result.provenance.confirmed is False
    assert result.argv is not None and result.argv[-2:] == ["--model", "claude-opus-4-8"]


async def test_launch_model_default_path_without_model() -> None:
    # model=None and no descriptor default: no --model flag, no selection claim.
    desc = AgentDescriptor("cursorish", "Cursorish", FAKE_ACP_CMD, model_launch_flag="--model")
    result = await run_acp_turn(desc, "MODEL?", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0)
    assert result.ok is True
    assert result.argv is not None and "--model" not in result.argv
    assert "launch_model=(unset)" in result.text
    assert result.selected_model is None
    assert result.provenance is not None and result.provenance.confirmed is False


def test_model_config_option_matches_by_category_and_keeps_only_string_values() -> None:
    from acp.schema import SessionConfigOptionBoolean, SessionConfigOptionSelect, SessionConfigSelectOption

    from rutherford.acp.session import _model_config_option

    # A boolean option (no value list) is skipped; the select is matched by category "model" even when its id
    # is NOT literally "model"; only string option values are kept.
    boolean = SessionConfigOptionBoolean(id="fast", name="Fast", type="boolean", current_value=False)
    select = SessionConfigOptionSelect(
        id="ai_model",
        name="Model",
        type="select",
        current_value="default",
        options=[
            SessionConfigSelectOption(name="Default", value="default"),
            SessionConfigSelectOption(name="Sonnet", value="sonnet"),
        ],
        category="model",
    )
    assert _model_config_option([boolean, select]) == ("ai_model", "default", ["default", "sonnet"])
    # No model option among them -> None (a boolean alone is not the model channel).
    assert _model_config_option([boolean]) is None
    # Fallback match on a literal id "model" when no category is set.
    by_id = SessionConfigOptionSelect(
        id="model",
        name="Model",
        type="select",
        current_value="haiku",
        options=[SessionConfigSelectOption(name="Haiku", value="haiku")],
    )
    assert _model_config_option([by_id]) == ("model", "haiku", ["haiku"])
    # A category-tagged option is AUTHORITATIVE over a literal id="model" fallback even when the id option is
    # advertised FIRST -- precedence must not depend on advertised order.
    id_first = SessionConfigOptionSelect(
        id="model",
        name="Mode-ish",
        type="select",
        current_value="x",
        options=[SessionConfigSelectOption(name="X", value="x")],
    )
    cat_second = SessionConfigOptionSelect(
        id="ai_model",
        name="Model",
        type="select",
        current_value="default",
        options=[SessionConfigSelectOption(name="Default", value="default")],
        category="model",
    )
    assert _model_config_option([id_first, cat_second]) == ("ai_model", "default", ["default"])


# --- Bedrock/Vertex model-env normalization (host_env.claude_bedrock_env) ------


_CLAUDE_SEAT = AgentDescriptor(
    "claude_code", "Claude Code", FAKE.command, provider="anthropic", underlying_cli="claude"
)


async def test_bedrock_claude_seat_gets_a_valid_model_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end: on a Bedrock host, Rutherford promotes ANTHROPIC_DEFAULT_OPUS_MODEL to ANTHROPIC_MODEL in the
    # SPAWNED subprocess env, so the claude-agent-acp adapter no longer falls back to the bare cloud alias. The
    # fake agent echoes its own ANTHROPIC_MODEL to prove the injection reached the subprocess.
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "us.anthropic.claude-opus-4-1-20250805-v1:0")
    result = await run_acp_turn(
        _CLAUDE_SEAT, "ENV=ANTHROPIC_MODEL", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0
    )
    assert result.ok is True
    assert "ANTHROPIC_MODEL=us.anthropic.claude-opus-4-1-20250805-v1:0" in result.text


async def test_non_bedrock_claude_seat_injects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gate: with no Bedrock/Vertex flag, behavior is identical to today -- no model is forced into the env,
    # even when an ANTHROPIC_DEFAULT_OPUS_MODEL is present (a normal API-key seat is untouched).
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "us.anthropic.should-not-be-used:0")
    result = await run_acp_turn(
        _CLAUDE_SEAT, "ENV=ANTHROPIC_MODEL", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=60.0
    )
    assert result.ok is True and "ANTHROPIC_MODEL=(unset)" in result.text


async def test_run_turn_handshake_failure() -> None:
    dead = AgentDescriptor("dead", "Dead", (sys.executable, "-c", "import sys; sys.exit(0)"))
    result = await run_acp_turn(dead, "hi", policy=_READ_ONLY, cwd=str(REPO_ROOT), timeout_s=10.0)
    assert result.ok is False and result.error is not None
    assert result.error.code is ErrorCode.ACP_HANDSHAKE_FAILED
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE


async def test_acp_session_reuses_one_live_session_across_turns() -> None:
    async with ACPSession(FAKE, policy=_READ_ONLY, cwd=str(REPO_ROOT)) as session:
        session_id = session.session_id
        assert session_id is not None and session.target.cli == "fake"
        first = await session.prompt("EMPTY please", timeout_s=60.0)
        assert first.ok is False  # the first turn produced no answer
        second = await session.prompt("what is 17 + 25?", timeout_s=60.0)
        assert second.ok is True and "42" in second.text  # second turn's journal is clean of the first
        assert session.session_id == session_id  # the same live session, not a re-spawn


async def test_acp_session_open_raises_on_bad_agent() -> None:
    bad = AgentDescriptor("bad", "Bad", ("this-binary-does-not-exist-xyz123",))
    session = ACPSession(bad, policy=_READ_ONLY, cwd=str(REPO_ROOT))
    with pytest.raises(ACPHandshakeError) as exc:
        await session.open()
    assert exc.value.code is ErrorCode.ACP_SPAWN_FAILED
    assert exc.value.safety is ReexecutionSafety.SAFE
