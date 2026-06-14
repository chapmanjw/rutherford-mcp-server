# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""A controllable in-process ACP agent for tests, run as ``python -m tests.fake_acp_agent``.

It implements just enough of the ACP ``Agent`` role to drive :func:`rutherford.acp.session.run_acp_turn`
without a real CLI. Behaviour is selected by trigger words in the prompt, so a test can exercise each path:
a normal answer (``17 + 25`` -> ``42``, else an echo), ``REFUSE`` (``stopReason`` refusal), ``EMPTY``
(a clean turn with no answer text), and ``HANG`` (a long sleep, for a timeout).
"""

from __future__ import annotations

import asyncio
from typing import Any

from acp import PROTOCOL_VERSION, run_agent
from acp.helpers import update_agent_message_text, update_agent_thought_text
from acp.schema import InitializeResponse, NewSessionResponse, PromptResponse


def _block_text(block: Any) -> str:
    return str(getattr(block, "text", "") or "")


class FakeAgent:
    """A deterministic ACP agent driven entirely by the prompt text."""

    def __init__(self) -> None:
        self._client: Any = None

    def on_connect(self, conn: Any) -> None:
        self._client = conn

    async def initialize(
        self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None, **kwargs: Any
    ) -> InitializeResponse:
        return InitializeResponse(protocol_version=PROTOCOL_VERSION)

    async def new_session(
        self, cwd: str, additional_directories: Any = None, mcp_servers: Any = None, **kwargs: Any
    ) -> NewSessionResponse:
        return NewSessionResponse(session_id="fake-session-1")

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def prompt(
        self, prompt: list[Any], session_id: str, message_id: str | None = None, **kwargs: Any
    ) -> PromptResponse:
        text = " ".join(_block_text(block) for block in prompt)
        if "HANG" in text:
            await asyncio.sleep(30)
        if "REFUSE" in text:
            return PromptResponse(stop_reason="refusal")
        if "EMPTY" in text:
            return PromptResponse(stop_reason="end_turn")
        answer = "42" if "17 + 25" in text else f"ECHO:{text[:40]}"
        await self._client.session_update(session_id, update_agent_thought_text("thinking"))
        await self._client.session_update(session_id, update_agent_message_text(answer))
        return PromptResponse(stop_reason="end_turn")


async def _main() -> None:
    await run_agent(FakeAgent())  # type: ignore[arg-type]  # a partial Agent: only the methods tests drive


if __name__ == "__main__":
    asyncio.run(_main())
