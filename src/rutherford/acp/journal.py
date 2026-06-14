# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The event journal: the canonical, event-sourced record of one ACP prompt turn.

Under ACP a turn's answer, cost, tool activity, and re-execution safety are all derived from the ordered
stream of ``session/update`` notifications plus the client's own decisions (permission grants, file
reads/writes) -- never scraped from stdout. This is the single source the reducer in :mod:`session`
projects a normalized result from, and the basis of the durable record. Append-only during a turn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..domain.models import Cost


@dataclass(slots=True)
class JournalEvent:
    """One ordered entry: an agent ``session/update`` kind, or a client-side decision.

    ``kind`` is the raw ``sessionUpdate`` discriminator (``agent_message_chunk``, ``tool_call``,
    ``usage_update``, ...) or a client decision (``permission_request``, ``fs_read``, ``fs_write``,
    ``fs_write_denied``, ``terminal_denied``). The remaining fields are populated only when relevant to
    ``kind``; ``ts`` is wall-clock epoch seconds stamped at construction.
    """

    kind: str
    text: str = ""
    tool_call_id: str | None = None
    status: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    detail: str = ""
    ts: float = field(default_factory=time.time)


@dataclass(slots=True)
class EventJournal:
    """The ordered journal of one turn. Appended to as the stream arrives; reduced after it ends."""

    events: list[JournalEvent] = field(default_factory=list)

    def append(self, event: JournalEvent) -> None:
        """Record one event in arrival order."""
        self.events.append(event)

    def message_text(self) -> str:
        """The agent's answer: every ``agent_message_chunk`` concatenated in order."""
        return "".join(event.text for event in self.events if event.kind == "agent_message_chunk")

    def thought_text(self) -> str:
        """The agent's reasoning trace: every ``agent_thought_chunk`` concatenated in order."""
        return "".join(event.text for event in self.events if event.kind == "agent_thought_chunk")

    def tool_call_count(self) -> int:
        """The number of distinct tool calls the agent reported (real topology, not a psutil floor)."""
        return len({event.tool_call_id for event in self.events if event.kind == "tool_call" and event.tool_call_id})

    def saw_side_effect(self) -> bool:
        """Whether a known external side effect (a served ``fs/write`` or a terminal) occurred."""
        return any(event.kind in ("fs_write", "terminal") for event in self.events)

    def saw_tool_activity(self) -> bool:
        """Whether the agent started any tool call or asked for a permission (its outcome is ambiguous)."""
        return any(event.kind in ("tool_call", "permission_request") for event in self.events)

    def usage(self) -> Cost | None:
        """The latest reported token usage as a :class:`Cost`, or ``None`` when the agent reported none."""
        for event in reversed(self.events):
            if event.kind != "usage_update":
                continue
            if event.input_tokens is None and event.output_tokens is None and event.total_tokens is None:
                return None
            total = event.total_tokens
            if total is None and event.input_tokens is not None and event.output_tokens is not None:
                total = event.input_tokens + event.output_tokens
            return Cost(input_tokens=event.input_tokens, output_tokens=event.output_tokens, total_tokens=total)
        return None

    def kinds(self) -> list[str]:
        """The ordered list of event kinds, for assertions and the durable record."""
        return [event.kind for event in self.events]


def _as_int(value: Any) -> int | None:
    """Coerce a usage token count to ``int``, or ``None`` when absent/unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def journal_event_from_message(message: dict[str, Any]) -> JournalEvent | None:
    """Build a :class:`JournalEvent` from a raw incoming ``session/update`` JSON-RPC message, or ``None``.

    Used by a SYNCHRONOUS stream observer so the journal is built in receive order, inline in the SDK's read
    loop -- and is therefore complete the moment the prompt response resolves the turn, since the agent's
    notifications precede that response in the stream (the alternative, the async ``Client.session_update``
    handler, runs on a separate dispatcher task and races the response). Reads the camelCase wire aliases the
    agent serializes (``sessionUpdate``, ``toolCallId``, ``inputTokens``, ...).
    """
    if message.get("method") != "session/update":
        return None
    params = message.get("params") or {}
    update = params.get("update") or {}
    kind = update.get("sessionUpdate")
    if not isinstance(kind, str):
        return None
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        content = update.get("content") or {}
        text = content.get("text", "") if isinstance(content, dict) else ""
        return JournalEvent(kind=kind, text=str(text or ""))
    if kind in ("tool_call", "tool_call_update"):
        status = update.get("status")
        return JournalEvent(
            kind=kind, tool_call_id=update.get("toolCallId"), status=str(status) if status is not None else None
        )
    if kind == "usage_update":
        return JournalEvent(
            kind=kind,
            input_tokens=_as_int(update.get("inputTokens")),
            output_tokens=_as_int(update.get("outputTokens")),
            total_tokens=_as_int(update.get("totalTokens")),
        )
    return JournalEvent(kind=kind)
