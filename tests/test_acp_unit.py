# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Unit tests for the ACP runtime pieces: journal, permission, descriptors, and the client callbacks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import PermissionOption

from rutherford.acp.client import RutherfordACPClient
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry, default_registry
from rutherford.acp.journal import EventJournal, JournalEvent, journal_event_from_message
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPSession, run_acp_turn
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode


def _client(mode: SafetyMode, cwd: str = ".") -> tuple[RutherfordACPClient, EventJournal]:
    journal = EventJournal()
    return RutherfordACPClient(journal=journal, policy=PermissionPolicy(mode), cwd=cwd), journal


def test_journal_message_thought_usage_kinds() -> None:
    journal = EventJournal()
    journal.append(JournalEvent(kind="agent_message_chunk", text="Hello "))
    journal.append(JournalEvent(kind="agent_thought_chunk", text="hmm"))
    journal.append(JournalEvent(kind="agent_message_chunk", text="world"))
    journal.append(JournalEvent(kind="usage_update", input_tokens=10, output_tokens=20))
    assert journal.message_text() == "Hello world"
    assert journal.thought_text() == "hmm"
    cost = journal.usage()
    assert cost is not None and cost.input_tokens == 10 and cost.output_tokens == 20 and cost.total_tokens == 30
    assert "usage_update" in journal.kinds()


def test_journal_usage_none_and_explicit_total() -> None:
    assert EventJournal().usage() is None
    empty = EventJournal()
    empty.append(JournalEvent(kind="usage_update"))
    assert empty.usage() is None
    explicit = EventJournal()
    explicit.append(JournalEvent(kind="usage_update", input_tokens=5, total_tokens=99))
    cost = explicit.usage()
    assert cost is not None and cost.total_tokens == 99


def test_journal_tool_count_and_side_effects() -> None:
    journal = EventJournal()
    journal.append(JournalEvent(kind="tool_call", tool_call_id="t1"))
    journal.append(JournalEvent(kind="tool_call", tool_call_id="t1"))
    journal.append(JournalEvent(kind="fs_write", detail="x"))
    assert journal.tool_call_count() == 1
    assert journal.saw_side_effect() is True
    assert journal.saw_tool_activity() is True
    assert EventJournal().saw_side_effect() is False


def test_permission_select_and_properties() -> None:
    options = [
        PermissionOption(kind="allow_once", name="Allow", option_id="a"),
        PermissionOption(kind="reject_once", name="Reject", option_id="r"),
    ]
    assert PermissionPolicy(SafetyMode.WRITE).select_permission(options) == "a"
    assert PermissionPolicy(SafetyMode.READ_ONLY).select_permission(options) == "r"
    assert PermissionPolicy(SafetyMode.READ_ONLY).select_permission([]) is None
    prefer = [
        PermissionOption(kind="allow_always", name="Always", option_id="aa"),
        PermissionOption(kind="allow_once", name="Once", option_id="a1"),
    ]
    assert PermissionPolicy(SafetyMode.YOLO).select_permission(prefer) == "a1"
    assert PermissionPolicy(SafetyMode.READ_ONLY).allow_writes is False
    assert PermissionPolicy(SafetyMode.WRITE).allow_writes is True
    assert PermissionPolicy(SafetyMode.PROPOSE).allow_tool_calls is False
    assert PermissionPolicy(SafetyMode.READ_ONLY).allow_fs_read is True


def test_descriptor_registry() -> None:
    registry = default_registry()
    assert registry.has("goose") and "goose" in registry.ids()
    assert registry.get("goose").command == ("goose", "acp")
    assert len(registry) == 16
    with pytest.raises(KeyError):
        registry.get("nope")
    with pytest.raises(ValueError, match="duplicate"):
        DescriptorRegistry([AgentDescriptor("x", "X", ("x",)), AgentDescriptor("x", "X2", ("y",))])


def test_session_resolves_relative_cwd_to_absolute() -> None:
    # ACP requires an absolute cwd in session/new; a relative one must be resolved before open().
    session = ACPSession(default_registry().get("goose"), policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=".")
    assert Path(session._cwd).is_absolute()


async def test_file_working_dir_is_a_clean_spawn_failure(tmp_path: Path) -> None:
    # A working_dir that resolves to a file (NotADirectoryError) is a launch failure, not an INTERNAL error.
    file_cwd = tmp_path / "not-a-dir.txt"
    file_cwd.write_text("x", encoding="utf-8")
    descriptor = AgentDescriptor("fake", "Fake", (sys.executable, "-c", "pass"))
    result = await run_acp_turn(
        descriptor, "hi", policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(file_cwd), timeout_s=10.0
    )
    assert result.ok is False
    assert result.error is not None and result.error.code is ErrorCode.ACP_SPAWN_FAILED


def test_official_adapter_descriptors() -> None:
    """The two official Zed adapters launch via their shim and carry the right fixed vendor."""
    registry = default_registry()
    codex = registry.get("codex")
    assert codex.command == ("codex-acp",) and codex.provider == "openai"
    claude = registry.get("claude_code")
    assert claude.command == ("claude-agent-acp",) and claude.provider == "anthropic"


def test_second_wave_descriptors() -> None:
    """copilot/qwen/droid (probed live) launch with their verified ACP commands."""
    registry = default_registry()
    assert registry.get("copilot").command == ("copilot", "--acp")
    assert registry.get("copilot").provider is None  # bring-your-own-model
    assert registry.get("qwen").command == ("qwen", "--acp") and registry.get("qwen").provider == "alibaba"
    assert registry.get("droid").command == ("droid", "exec", "--output-format", "acp")
    assert registry.get("droid").provider is None
    assert registry.get("hermes").command == ("hermes", "acp") and registry.get("hermes").provider == "nous"


def test_third_wave_descriptors() -> None:
    """cursor/kiro (probed live with the docs-confirmed commands) launch over ACP."""
    registry = default_registry()
    # cursor's `acp` subcommand is hidden from --help but real; cursor-agent IS the `agent` binary.
    assert registry.get("cursor").command == ("cursor-agent", "acp")
    # kiro's ACP binary is kiro-cli (the `kiro` binary is the IDE launcher).
    assert registry.get("kiro").command == ("kiro-cli", "acp")
    # pi runs through the pi-acp wrapper (npm pi-acp), which spawns `pi --mode rpc`.
    assert registry.get("pi").command == ("pi-acp",) and registry.get("pi").provider is None


def test_journal_event_from_message() -> None:
    def msg(update: dict[str, object]) -> dict[str, object]:
        return {"method": "session/update", "params": {"sessionId": "s", "update": update}}

    assert journal_event_from_message({"method": "other"}) is None
    assert journal_event_from_message(msg({"sessionUpdate": 123})) is None
    text_event = journal_event_from_message(
        msg({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}})
    )
    assert text_event is not None and text_event.kind == "agent_message_chunk" and text_event.text == "hi"
    tool_event = journal_event_from_message(msg({"sessionUpdate": "tool_call", "toolCallId": "t1", "status": "ok"}))
    assert tool_event is not None and tool_event.tool_call_id == "t1" and tool_event.status == "ok"
    usage_event = journal_event_from_message(
        msg({"sessionUpdate": "usage_update", "inputTokens": 3, "outputTokens": 4, "totalTokens": 7})
    )
    assert usage_event is not None and usage_event.input_tokens == 3 and usage_event.total_tokens == 7
    other_event = journal_event_from_message(msg({"sessionUpdate": "available_commands_update"}))
    assert other_event is not None and other_event.kind == "available_commands_update"


async def test_client_session_update_is_a_noop_sink() -> None:
    client, journal = _client(SafetyMode.READ_ONLY)
    client.on_connect(object())
    await client.session_update("s", object())  # the observer journals the stream; the handler is a no-op
    assert journal.kinds() == []


async def test_client_permission_allow_and_cancel() -> None:
    options = [
        PermissionOption(kind="allow_once", name="Allow", option_id="a"),
        PermissionOption(kind="reject_once", name="Reject", option_id="r"),
    ]
    write_client, _ = _client(SafetyMode.WRITE)
    allowed = await write_client.request_permission(options, "s", None)
    assert allowed.outcome.outcome == "selected"
    read_client, journal = _client(SafetyMode.READ_ONLY)
    denied = await read_client.request_permission([], "s", None)
    assert denied.outcome.outcome == "cancelled"
    assert "permission_request" in journal.kinds()


async def test_client_read_and_write(tmp_path: object) -> None:
    base = tmp_path
    src = base / "a.txt"  # type: ignore[operator]
    src.write_text("l1\nl2\nl3\n", encoding="utf-8")
    client, journal = _client(SafetyMode.READ_ONLY, cwd=str(base))
    whole = await client.read_text_file(str(src), "s")
    assert whole.content == "l1\nl2\nl3\n"
    windowed = await client.read_text_file(str(src), "s", limit=1, line=2)
    assert windowed.content == "l2\n"
    with pytest.raises(RequestError):
        await client.read_text_file(str(base / "missing.txt"), "s")  # type: ignore[operator]
    with pytest.raises(RequestError):
        await client.write_text_file("data", str(base / "b.txt"), "s")  # type: ignore[operator]
    assert "fs_write_denied" in journal.kinds()
    write_client, write_journal = _client(SafetyMode.WRITE, cwd=str(base))
    await write_client.write_text_file("data", str(base / "b.txt"), "s")  # type: ignore[operator]
    assert (base / "b.txt").read_text(encoding="utf-8") == "data"  # type: ignore[operator]
    assert "fs_write" in write_journal.kinds()


async def test_client_terminal_and_ext_declined() -> None:
    client, _ = _client(SafetyMode.WRITE)
    for coro in (
        client.create_terminal("ls", "s"),
        client.terminal_output("s", "t"),
        client.wait_for_terminal_exit("s", "t"),
        client.kill_terminal("s", "t"),
        client.release_terminal("s", "t"),
        client.ext_method("m", {}),
    ):
        with pytest.raises(RequestError):
            await coro
    await client.ext_notification("m", {})  # a notification: no response, must not raise
