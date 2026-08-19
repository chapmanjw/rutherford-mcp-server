# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""What Rutherford does with payloads the SDK's own serializer would never produce.

ACP 0.12.0 introduced lenient deserialization: a field that fails validation is salvaged into its raw form
rather than raising, and a list item that fails is skipped while the rest of the list parses. That removes
validation this client was implicitly relying on. ``InitializeResponse.agent_capabilities`` is annotated
``Optional[AgentCapabilities]`` and can now hold a ``dict``, so an attribute read on it raises AttributeError
in the middle of the handshake -- where nothing catches it and the spawned agent is left running.

Every test here drives ``tests/raw_acp_agent.py`` rather than ``tests/fake_acp_agent.py``. The reason is in
that module's docstring and it is the whole point of this file: the fake agent shares the installed SDK's
codec with the client, so it physically cannot emit a frame that triggers salvage or skip. These tests write
the bytes themselves.

Layered on purpose, because each layer proves something the others cannot:

* End to end through ``run_acp_turn`` -- the production entry point, a real subprocess, the real transport,
  the real teardown. This is the only layer that can answer the orphan question.
* At the helper (``_model_config_option``, ``_load_session_capability``) with values built by hand -- pins the
  contract for shapes the one payload on the wire does not happen to cover.
* Against the SDK's own validator with no Rutherford in the picture -- pins the 0.12.0 semantics themselves,
  so a later release that re-tightens or loosens further fails here first and names the cause, instead of
  surfacing as an unrelated end-to-end test behaving oddly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from asyncio.subprocess import Process
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
from acp import spawn_agent_process as sdk_spawn_agent_process
from acp.schema import NewSessionResponse, SessionConfigOptionSelect, SessionConfigSelectOption

from rutherford.acp.descriptors import AgentDescriptor
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import (
    ACPSession,
    _load_session_capability,
    _model_config_option,
    _option_category,
    run_acp_turn,
)
from rutherford.domain.enums import ReexecutionSafety, SafetyMode
from rutherford.domain.error_codes import ErrorCode

RAW_AGENT = Path(__file__).resolve().parent / "raw_acp_agent.py"
#: Distinguishes "the test passed no agentCapabilities key at all" from "the test passed null", which the
#: SDK answers differently (field default vs None) and which the capability read must not conflate.
_ABSENT = object()
_READ_ONLY = PermissionPolicy(SafetyMode.READ_ONLY)
#: Generous enough that a cold interpreter start on a loaded CI runner is never mistaken for a hang, short
#: enough that a genuine hang fails the cell rather than sitting on the job timeout.
_TURN_TIMEOUT_S = 20.0


# --- the harness ------------------------------------------------------------------------------------------


def _raw_agent(
    tmp_path: Path,
    script: dict[str, Any],
    *,
    agent_id: str = "raw",
    default_model: str | None = None,
) -> tuple[AgentDescriptor, Path]:
    """Write ``script`` to disk and return the descriptor that launches the raw-wire agent against it.

    The transcript path comes back alongside the descriptor because several assertions below are about what
    Rutherford did NOT send, and a negative is only checkable against a record the agent itself kept -- the
    client side has nothing to show for a request it never made.
    """
    script_path = tmp_path / f"{agent_id}-script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    transcript = tmp_path / f"{agent_id}-calls.jsonl"
    transcript.write_text("", encoding="utf-8")
    descriptor = AgentDescriptor(
        agent_id,
        "Raw wire agent",
        (sys.executable, str(RAW_AGENT), str(script_path), str(transcript)),
        default_model=default_model,
    )
    return descriptor, transcript


def _methods(transcript: Path) -> list[str]:
    """The ACP methods the agent actually received, in arrival order."""
    lines = transcript.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["method"] for line in lines if line.strip()]


def _params(transcript: Path, method: str) -> list[Any]:
    """Every params object the agent received for ``method``, in arrival order."""
    lines = transcript.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    return [entry["params"] for entry in entries if entry["method"] == method]


def _spawned_agents(monkeypatch: pytest.MonkeyPatch) -> list[Process]:
    """Capture the real subprocess handle for every agent spawned during the test.

    It cannot be read off the session after the fact: ``ACPSession.close`` clears ``_process`` in its
    ``finally``, so on every path where teardown DID run -- most of them, and precisely what these tests set
    out to confirm -- the handle is already gone by the time an assertion could look at it. Wrapping the
    spawn context manager keeps a reference to the same object the session held, and keeps it on the failing
    paths too, where the session itself may never become reachable.
    """
    spawned: list[Process] = []

    @contextlib.asynccontextmanager
    async def spawn_spy(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        # * The SDK function is taken from ``acp`` rather than through the module being patched, the way
        # ``tests/test_killpath.py`` does it: reading it back off the module after monkeypatch has replaced
        # the name is how a spy ends up recursing into itself.
        async with sdk_spawn_agent_process(*args, **kwargs) as (conn, process):
            spawned.append(process)
            yield conn, process

    monkeypatch.setattr("rutherford.acp.session.spawn_agent_process", spawn_spy)
    return spawned


def _capture_sessions(monkeypatch: pytest.MonkeyPatch) -> list[ACPSession]:
    """Record every session ``run_acp_turn`` opens, for the few assertions about what the client BELIEVES.

    Spying on ``open`` rather than ``__init__`` keeps the capture on the class the production path actually
    calls, the same technique ``tests/test_killpath.py`` uses to catch a session at the moment it is live.
    Used sparingly: state the client merely believes (which models it thinks the agent offers) is only
    readable here, but anything observable on the wire is asserted from the transcript instead.
    """
    opened: list[ACPSession] = []
    original = ACPSession.open

    async def open_spy(self: ACPSession) -> None:
        opened.append(self)
        await original(self)

    monkeypatch.setattr(ACPSession, "open", open_spy)
    return opened


async def _assert_no_orphan(spawned: list[Process]) -> None:
    """Fail unless every agent spawned during the test is genuinely dead.

    Asserted the way ``tests/test_killpath.py`` asserts a brokered terminal died -- wait on the real process
    handle and let the wait time out -- rather than sampling ``poll()`` once, because a teardown that is
    merely slow is not a leak and one sample cannot tell the two apart. The raw agent runs until stdin EOF
    and never exits on its own, so a handle that never resolves means nothing ever closed the transport: an
    exception escaped ``open``, ``async with`` skipped ``__aexit__``, and the spawned tree was stranded.
    """
    assert spawned, "no agent was spawned, so this test proved nothing about orphaning"
    for process in spawned:
        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except TimeoutError:
            raise AssertionError(
                "the agent subprocess outlived the failed handshake: an exception escaped open() past its "
                "per-stage guards, so nothing tore the spawned agent down"
            ) from None
        finally:
            # A test about leaked processes must never leak the process it polices.
            if process.returncode is None:  # pragma: no cover - reached only when the assertion above failed
                process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()


# --- the harness itself: it has to work when nothing is wrong ---------------------------------------------


async def test_the_raw_agent_drives_a_clean_handshake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-written frame is only evidence if the fixture is right when the payload is not the problem.

    Without this, every red test below has two candidate explanations -- Rutherford mishandled the payload,
    or the fixture never spoke ACP properly -- and the second is the cheaper story to believe. An empty
    script exercises the defaults end to end, so a failure anywhere else in this file is about the payload
    under test rather than about the harness.
    """
    descriptor, transcript = _raw_agent(tmp_path, {})
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S)

    assert result.ok is True
    assert result.text == "raw-ok"
    assert result.session_id == "raw-session-1"
    assert _methods(transcript) == ["initialize", "session/new", "session/prompt"]
    await _assert_no_orphan(spawned)


async def test_an_unanswerable_method_fails_fast_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script omission has to read as a script omission, not as a mysterious 30-second handshake timeout.

    The raw agent answers an unscripted method with JSON-RPC -32601 for exactly this reason. Pinned because
    it is a diagnostic property of the fixture rather than a detail of it: the day someone simplifies the
    agent to ignore unknown methods, every later mistake in this file starts costing a full handshake budget
    and reporting the wrong cause.
    """
    descriptor, _transcript = _raw_agent(tmp_path, {"drop": ["initialize"]})
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.ACP_HANDSHAKE_FAILED
    await _assert_no_orphan(spawned)


# --- R1: a salvaged agentCapabilities ---------------------------------------------------------------------
#
# 0.12.0 salvages a malformed ``agentCapabilities`` into a raw dict instead of raising, so the value violates
# its own ``Optional[AgentCapabilities]`` annotation: it is not None, and it has no ``load_session``
# attribute. mypy cannot see this (it trusts the annotation) and the fake agent cannot produce it (it always
# builds a real ``AgentCapabilities``), so the only thing between this and production is a test that writes
# the bytes itself.

_MALFORMED_CAPS: dict[str, Any] = {
    "results": {"initialize": {"protocolVersion": 1, "agentCapabilities": "broken"}},
}


async def test_a_salvaged_capability_blob_is_a_clean_resume_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capability blob Rutherford cannot read means "cannot resume", not an AttributeError mid-handshake.

    ``_resume`` runs OUTSIDE the per-stage ``except Exception`` that wraps only ``conn.initialize``; the sole
    outer handler catches ``asyncio.CancelledError``, and ``run_acp_turn`` catches only ``ACPHandshakeError``.
    So an AttributeError here never becomes a failed result -- it escapes the delegation entirely AND strands
    the spawned agent, because Python skips ``__aexit__`` when ``__aenter__`` raises. That is the leaked-agent
    class this module's teardown exists to prevent, reached through a payload no existing test could produce.
    Both halves are asserted: the classified failure, and the dead process.
    """
    descriptor, transcript = _raw_agent(tmp_path, _MALFORMED_CAPS)
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(
        descriptor,
        "hello",
        policy=_READ_ONLY,
        cwd=str(tmp_path),
        timeout_s=_TURN_TIMEOUT_S,
        resume_session_id="prior-session",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.RESUME_FAILED
    assert result.error.reexecution_safety is ReexecutionSafety.SAFE
    # Says WHY, and says the right why. An agent that genuinely does not implement session reload gets the
    # other message; conflating the two would send an operator off to check a capability their agent never
    # had a chance to advertise, when the real defect is that its handshake payload is malformed.
    assert "malformed agentCapabilities" in result.error.message
    # Refused BEFORE the RPC, not attempted and failed: an agent whose capabilities did not survive parsing
    # has told us nothing about whether it can reload a session, so asking it to would be a guess.
    assert "session/load" not in _methods(transcript)
    await _assert_no_orphan(spawned)


async def test_a_salvaged_capability_blob_does_not_break_an_ordinary_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capabilities are read only to decide a resume, so a salvaged blob must not cost a normal delegation.

    Without this, the obvious fix -- reject any initialize whose capabilities did not validate -- looks
    correct and would bench every seat whose adapter advertises a capability field this SDK version does not
    model yet. Leniency in the SDK is exactly what makes such an adapter workable; the guard belongs at the
    one read that depends on the value, not at the handshake.
    """
    descriptor, transcript = _raw_agent(tmp_path, _MALFORMED_CAPS)
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S)

    assert result.ok is True
    assert result.text == "raw-ok"
    # Spelled as the exact sequence rather than a membership check: the point is that a salvaged capability
    # blob provoked no extra probing, no retry and no second handshake, none of which a membership check sees.
    assert _methods(transcript) == ["initialize", "session/new", "session/prompt"]
    await _assert_no_orphan(spawned)


def test_load_session_capability_reads_every_response_shape() -> None:
    """The read is pinned directly, because what salvage yields is the SDK's choice rather than ours.

    Three outcomes, deliberately not two. "The agent answered and the answer was no" and "the agent's
    capability blob was garbage" are different facts, and the caller turns them into different messages --
    telling an operator their agent does not support resume, when the truth is that it is emitting invalid
    protocol, sends them to debug the wrong thing.

    The ``dict`` case reports UNKNOWN rather than being mined for its ``loadSession`` key, and that asymmetry
    is the load-bearing part. Measured against the 0.12.0 wheel (see the table this pins below), the attribute
    is a ``dict`` only when the whole field failed validation, and what the SDK substitutes then is a
    hard-coded literal whose ``loadSession`` is always ``False`` regardless of what the agent sent. A mapping
    the agent really supplied coerces to a genuine ``AgentCapabilities``, salvaging per-field. So reading the
    dict could only ever report the SDK's placeholder as though it were the agent's own answer.
    """
    assert _load_session_capability(SimpleNamespace(agent_capabilities=SimpleNamespace(load_session=True))) is True
    assert _load_session_capability(SimpleNamespace(agent_capabilities=SimpleNamespace(load_session=False))) is False
    # An explicit null and a response omitting the field entirely are both a plain "no": advertising nothing
    # is not the same as being unreadable, and an agent that advertises nothing supports nothing.
    assert _load_session_capability(SimpleNamespace(agent_capabilities=None)) is False
    assert _load_session_capability(SimpleNamespace()) is False
    # The 0.12 salvage fallback. Its contents say False, but that is the SDK talking, not the agent.
    assert _load_session_capability(SimpleNamespace(agent_capabilities={"loadSession": False})) is None
    assert _load_session_capability(SimpleNamespace(agent_capabilities={"loadSession": True})) is None
    # A non-bool advertisement is unreadable too, rather than being coerced by truthiness.
    assert _load_session_capability(SimpleNamespace(agent_capabilities=SimpleNamespace(load_session="yes"))) is None


def test_the_sdk_salvages_only_a_non_mapping_capability_blob() -> None:
    """The measurement the tri-state rests on, pinned so a future SDK cannot quietly invalidate it.

    :func:`_load_session_capability` treats a ``dict`` as unreadable *because* a dict can only be the SDK's
    hard-coded substitute. If a later release started passing an agent's own mapping through unvalidated,
    that reasoning would silently become wrong and a resumable agent would start reporting as unreadable.
    This is the tripwire for that, run against the SDK's own validator with no Rutherford in the picture.
    """
    from acp.schema import AgentCapabilities, InitializeResponse

    def _caps(value: object) -> object:
        payload: dict[str, Any] = {"protocolVersion": 1}
        if value is not _ABSENT:
            payload["agentCapabilities"] = value
        return InitializeResponse.model_validate(payload).agent_capabilities

    # A non-mapping cannot be coerced at all, so the whole field is replaced by the literal fallback.
    non_mappings: list[object] = ["broken", [], 7]
    for non_mapping in non_mappings:
        salvaged = _caps(non_mapping)
        assert isinstance(salvaged, dict), non_mapping
        assert salvaged["loadSession"] is False, "the fallback is a fixed literal, not the agent's answer"
    # A mapping the agent supplied still becomes a real model, with per-field salvage inside it -- which is
    # exactly why mining a dict for loadSession would never recover a genuine advertisement.
    assert isinstance(_caps({"loadSession": True}), AgentCapabilities)
    assert isinstance(_caps({"loadSession": True, "promptCapabilities": "nonsense"}), AgentCapabilities)
    coerced = _caps({"loadSession": "not-a-bool"})
    assert isinstance(coerced, AgentCapabilities)  # narrows for the strict type checker, and is the claim
    assert coerced.load_session is False
    # Absent yields the field default (a real model); an explicit null stays None.
    assert isinstance(_caps(_ABSENT), AgentCapabilities)
    assert _caps(None) is None


# --- R2: an object-form config-option category ------------------------------------------------------------
#
# 0.12.0 widened ``SessionConfigOptionSelect.category`` from ``Optional[str]`` to
# ``Optional[Union[str, Dict[str, Any]]]``, so an object-valued category PARSES where 0.11.0 rejected the
# whole payload and raised it as a loud ACP_HANDSHAKE_FAILED. ``_option_category`` narrows that back to a
# string and reports anything else as untagged, which is what the spec asks of clients ("MUST handle missing
# or unknown categories gracefully") and what keeps the reader able to tell a deliberate no-match from an
# unhandled type. Two consequences follow, and both need pinning over the wire -- neither is reachable from
# the SDK-backed fixture, because its serializer cannot emit an object here at all.

_OBJECT_CATEGORY: dict[str, Any] = {"type": "model", "label": "Model"}


def _object_category_script(option_id: str, echo: str) -> dict[str, Any]:
    """An agent whose model select carries an OBJECT category, echoing ``echo`` back once it is selected."""
    advertised = {
        "id": option_id,
        "name": "Model",
        "type": "select",
        "category": _OBJECT_CATEGORY,
        "currentValue": "sonnet",
        "options": [{"name": "Sonnet", "value": "sonnet"}, {"name": "Opus", "value": "opus"}],
    }
    return {
        "results": {
            "session/new": {"sessionId": "raw-session-1", "configOptions": [advertised]},
            "session/set_config_option": {"configOptions": [{**advertised, "currentValue": echo}]},
        }
    }


async def test_an_object_category_no_longer_kills_the_handshake_and_the_id_fallback_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic adapter shape: an unreadable category, but an option keyed ``model``.

    Two things at once, because on the wire they are one event. First, the payload no longer fails the
    handshake -- on 0.11.0 an object category made ``session/new`` unparseable and benched the seat, which is
    the regression a version bump would otherwise smuggle in either direction. Second, an unreadable category
    is treated as UNTAGGED rather than as a mismatch, so the ``id == "model"`` fallback still reaches the
    genuine model channel and the selection is confirmed. Get the second part wrong and every adapter that
    ever emits an object here loses model selection while continuing to look perfectly healthy.
    """
    descriptor, transcript = _raw_agent(tmp_path, _object_category_script("model", "opus"))
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(
        descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S, model="opus"
    )

    assert result.ok is True
    assert result.provenance is not None
    assert result.provenance.model == "opus"
    assert result.provenance.confirmed is True
    selected = _params(transcript, "session/set_config_option")
    assert len(selected) == 1
    assert selected[0]["configId"] == "model"
    assert selected[0]["value"] == "opus"
    await _assert_no_orphan(spawned)


async def test_an_object_category_on_a_non_model_id_is_an_honest_model_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted cost of narrowing to ``str``, recorded rather than left to be rediscovered.

    Here the object category is the ONLY thing that could have identified the channel, and it is unreadable,
    so the option is invisible as a model channel and an explicitly named model fails MODEL_UNAVAILABLE. That
    is deliberate: the alternative is guessing at the shape of an object no published ACP schema defines, and
    a wrong guess drives ``session/set_config_option`` against a selector that is not the model. Pinned so
    the tradeoff stays a decision. If a real adapter is ever found emitting this, THIS is the test that has
    to change, and it says so.
    """
    descriptor, transcript = _raw_agent(tmp_path, _object_category_script("anthropic_model", "opus"))
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(
        descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S, model="opus"
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.MODEL_UNAVAILABLE
    assert "session/set_config_option" not in _methods(transcript)  # never guessed at the wrong selector
    await _assert_no_orphan(spawned)


def test_option_category_narrows_anything_that_is_not_a_string_to_untagged() -> None:
    """The narrowing pinned at the helper, for the shapes one frame on the wire cannot cover.

    The dict is the shape 0.12.0 actually produces; the rest are there because ``Dict[str, Any]`` is only the
    generator's guess at a multi-variant ``anyOf`` and the next regeneration could widen it again. Reading a
    string as a string and everything else as untagged is stable under all of them.
    """
    string = SessionConfigOptionSelect(
        id="ai_model",
        name="Model",
        type="select",
        current_value="sonnet",
        options=[SessionConfigSelectOption(name="Sonnet", value="sonnet")],
        category="model",
    )
    assert _option_category(string) == "model"
    for unreadable in ({"type": "model"}, {}, 7, ["model"]):
        assert _option_category(SimpleNamespace(category=unreadable)) is None, unreadable
    assert _option_category(SimpleNamespace()) is None  # an option carrying no category field at all


def test_an_unreadable_category_does_not_disqualify_the_id_fallback() -> None:
    """Untagged must mean untagged, not "tagged as something else".

    ``_model_config_option`` disqualifies an ``id == "model"`` option whose category names a DIFFERENT
    channel. An object category has to land on the untagged side of that check rather than the disqualified
    side: it is an unreadable value, not a claim that this is a mode selector, and treating it as one would
    silently drop the model channel of any adapter that emits an object here.
    """
    option = SessionConfigOptionSelect(
        id="model",
        name="Model",
        type="select",
        current_value="sonnet",
        options=[SessionConfigSelectOption(name="Sonnet", value="sonnet")],
        category={"type": "mode"},  # unreadable, and would be disqualifying if it were the string "mode"
    )
    assert _model_config_option([option]) == ("model", "sonnet", ["sonnet"])


# --- R3: a skipped config option --------------------------------------------------------------------------
#
# 0.12.0 drops a ``configOptions`` item that fails validation and parses the rest, where 0.11.0 failed the
# whole payload and ``_new_session`` turned that into a loud ACP_HANDSHAKE_FAILED. The drop is silent: the
# client receives a shorter list with no signal that anything was removed. These record what that silence
# costs, so the loud-versus-quiet decision is taken against measurements rather than reasoning.

_DROPPED_MODEL_OPTION: dict[str, Any] = {
    "results": {
        "session/new": {
            "sessionId": "raw-session-1",
            # Invalid against BOTH members of the configOptions union: a select needs ``options``, and a
            # boolean needs a bool ``currentValue``. 0.11.0 rejected the whole session/new over this item;
            # 0.12.0 drops just this entry, leaving a response that parses cleanly with no model channel.
            "configOptions": [{"id": "model", "name": "Model", "type": "select", "currentValue": "sonnet"}],
        }
    }
}


def test_the_sdk_drops_an_invalid_config_option_without_saying_so() -> None:
    """The 0.12.0 behaviour this whole file exists for, pinned as a literal fact rather than an assumption.

    Run through the SDK's own validator with no Rutherford in the picture. This is the one thing the cheap
    "parse a dict through the model" approach genuinely proves, so it is used for exactly that and nothing
    else: if a later release re-tightens, or loosens further, this fails first and names the cause instead of
    the change surfacing as an unrelated end-to-end test behaving oddly.
    """
    parsed = NewSessionResponse.model_validate(
        {
            "sessionId": "s1",
            "configOptions": [
                {
                    "id": "effort",
                    "name": "Effort",
                    "type": "select",
                    "currentValue": "high",
                    "options": [{"name": "High", "value": "high"}],
                },
                {"id": "model", "name": "Model", "type": "select", "currentValue": "sonnet"},  # no options
            ],
        }
    )

    assert parsed.config_options is not None
    assert [option.id for option in parsed.config_options] == ["effort"]


async def test_a_dropped_model_option_leaves_a_descriptor_default_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the silent drop costs a descriptor-default model, recorded end to end.

    On 0.11.0 this payload failed the whole handshake, and ACP_HANDSHAKE_FAILED is in the unhealthy set that
    benches a seat. On 0.12.0 the bad item vanishes, the response parses, the model channel merely appears to
    be absent, and the turn runs on whatever default the agent already had. The result still reports the
    requested id -- which is what consensus diversity keys on -- with ``confirmed`` False as the only trace
    that nobody verified it. Pinned so that trace cannot quietly disappear, and so that whichever way the
    loud-versus-quiet call goes, the change in behaviour lands in a diff rather than in production.
    """
    descriptor, transcript = _raw_agent(tmp_path, _DROPPED_MODEL_OPTION, default_model="opus")
    spawned = _spawned_agents(monkeypatch)
    opened = _capture_sessions(monkeypatch)

    result = await run_acp_turn(descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S)

    assert result.ok is True
    assert result.provenance is not None
    assert result.provenance.model == "opus"  # what the result CLAIMS the turn ran under ...
    assert result.provenance.confirmed is False  # ... and the single bit saying nobody checked
    assert "session/set_config_option" not in _methods(transcript)  # selection was never even attempted
    assert opened[0].available_models == []  # the channel the dropped item carried is simply gone
    await _assert_no_orphan(spawned)


async def test_a_dropped_model_option_fails_an_explicit_model_on_a_reachable_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sharp edge of the same drop: a caller-named model dies against an agent that advertises it.

    The agent DID advertise ``opus``; the item carrying it failed validation and was removed before
    Rutherford ever saw the response, and doctor will still call the seat reachable. This is also the
    negative control for the orphan assertions above -- ``_select_model`` raises inside ``open``'s inner
    guard, which closes before propagating -- so that path has to stay leak-free under the new payload class
    as well as the old one.
    """
    script = {
        "results": {
            "session/new": {
                "sessionId": "raw-session-1",
                "configOptions": [
                    {"id": "model", "name": "Model", "type": "select", "currentValue": "sonnet"},  # dropped
                ],
            }
        }
    }
    descriptor, transcript = _raw_agent(tmp_path, script)
    spawned = _spawned_agents(monkeypatch)

    result = await run_acp_turn(
        descriptor, "hello", policy=_READ_ONLY, cwd=str(tmp_path), timeout_s=_TURN_TIMEOUT_S, model="opus"
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.MODEL_UNAVAILABLE
    assert "session/prompt" not in _methods(transcript)
    await _assert_no_orphan(spawned)


# --- the teardown guard, structurally ----------------------------------------------------------------------


async def test_open_tears_down_the_agent_when_a_post_session_read_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the capability defect: the band between the session RPC and model selection.

    Fixing the capability read alone would leave the rest of ``open`` bare. The plain reads that follow
    ``session/new`` -- ``_models_of``, the config_options capture, ``_model_config_option`` -- used to sit
    inside no guard at all, which is precisely how ONE unguarded attribute access could strand an adapter:
    the exception escapes ``open``, ``run_acp_turn`` entered the session with ``async with``, and Python skips
    ``__aexit__`` when ``__aenter__`` raises.

    So the guard is asserted structurally rather than through the capability read that exposed it. Blowing up
    a read the code has no reason to expect proves the whole post-spawn band is covered, and it keeps failing
    if someone later narrows the ``try`` back down to the two selection calls. An exception type ``open``
    never anticipates is the point -- a test that raised ``ACPHandshakeError`` here would be caught by
    machinery that already existed and would prove nothing.
    """

    def _boom(_options: list[object]) -> NoReturn:
        raise RuntimeError("a response read blew up after the agent was spawned")

    monkeypatch.setattr("rutherford.acp.session._model_config_option", _boom)
    descriptor, _transcript = _raw_agent(tmp_path, {})
    spawned = _spawned_agents(monkeypatch)

    # Deliberately NOT through run_acp_turn: it catches only ACPHandshakeError, so a RuntimeError would
    # escape the delegation entirely. Driving the session directly is what isolates the teardown guarantee
    # from the question of who classifies the error.
    session = ACPSession(descriptor, policy=_READ_ONLY, cwd=str(tmp_path))
    with pytest.raises(RuntimeError):
        await session.open()

    await _assert_no_orphan(spawned)
