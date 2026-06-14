# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""A controllable in-process ACP agent for tests, run as ``python -m tests.fake_acp_agent``.

It implements just enough of the ACP ``Agent`` role to drive :func:`rutherford.acp.session.run_acp_turn`
without a real CLI. Behaviour is selected by trigger words in the prompt, so a test can exercise each path:
a normal answer (``17 + 25`` -> ``42``, else an echo), ``REFUSE`` (``stopReason`` refusal), ``EMPTY``
(a clean turn with no answer text), and ``HANG`` (a long sleep, for a timeout).

To drive the consensus strategies a test needs the answer text to carry a chosen verdict. A
``SAY=<text>`` token anywhere in the prompt makes the agent answer with exactly ``<text>`` (everything
after ``SAY=`` up to a newline), so a test can plant a ``VERDICT: yes`` line or a ``{"verdict": "no"}``
JSON object verbatim. Without a ``SAY=`` token the legacy echo behaviour is unchanged.

To drive the time-budget harvest a test needs a voice that runs LONG. A ``SLEEP=<seconds>`` token makes the
agent stream a short partial line and then sleep for ``<seconds>`` before finishing, so a panel deadline
shorter than the sleep cuts the voice in flight (and harvests the streamed partial). A bare ``SLEEP`` (no
``=``) sleeps a long default. The partial is streamed as an ``agent_message_chunk`` BEFORE the sleep, so a
cut voice's :attr:`~rutherford.acp.session.ACPSession.partial_text` is non-empty.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from acp import PROTOCOL_VERSION, run_agent
from acp.helpers import update_agent_message_text, update_agent_thought_text
from acp.schema import InitializeResponse, ModelInfo, NewSessionResponse, PromptResponse, SessionModelState


def _block_text(block: Any) -> str:
    return str(getattr(block, "text", "") or "")


def _planted_answer(text: str) -> str | None:
    """The answer a ``SAY=<text>`` token dictates (the rest of that line), or ``None`` when absent.

    Capturing only to the next newline keeps the appended verdict instruction (which the consensus
    service adds on a later line, and which itself contains the literal ``VERDICT: <token>``) out of the
    planted answer, so a test that plants ``SAY=VERDICT: yes`` gets exactly ``VERDICT: yes`` back.
    """
    marker = "SAY="
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    line, _, _ = rest.partition("\n")
    return line.strip()


def _sleep_seconds(text: str) -> float | None:
    """The seconds to sleep before answering: the ``RUTHERFORD_FAKE_SLEEP`` env, else a ``SLEEP=<n>`` token.

    The env var lets a test register a *slow* fake agent by descriptor (its own command/env), so a consensus
    panel can mix a fast voice and a slow voice without per-voice prompts -- the panel sends one shared prompt.
    A ``SLEEP=<n>`` token in the prompt is the alternative for a one-off slow turn (a bare ``SLEEP`` sleeps a
    long default). ``None`` when neither asks for a sleep.
    """
    env = os.environ.get("RUTHERFORD_FAKE_SLEEP")
    if env:
        try:
            return float(env)
        except ValueError:
            return 30.0
    marker = "SLEEP"
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    if rest.startswith("="):
        token, _, _ = rest[1:].partition("\n")
        try:
            return float(token.strip().split()[0])
        except (ValueError, IndexError):
            return 30.0
    return 30.0


def _advertised_models() -> SessionModelState | None:
    """The models this fake advertises at ``new_session``, from ``RUTHERFORD_FAKE_MODELS`` (comma-separated).

    Off by default (``None``) so the existing tests, which do not expect a ``session/set_model`` call, are
    unchanged: the client only sends ``set_model`` for a model the session advertised here. A test opts in by
    setting the env to the ids it wants selectable (e.g. ``gpt-5.2[high]`` for the codex effort path).
    """
    raw = os.environ.get("RUTHERFORD_FAKE_MODELS")
    if not raw:
        return None
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    if not ids:
        return None
    infos = [ModelInfo(model_id=model_id, name=model_id) for model_id in ids]
    return SessionModelState(available_models=infos, current_model_id=ids[0])


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
        models = _advertised_models()
        return NewSessionResponse(session_id="fake-session-1", models=models)

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        # Accepting any advertised id is enough for the client's best-effort set_model call to succeed (e.g. an
        # effort-rewritten 'gpt-5.2[high]'); the client suppresses any error, so an unknown id is safe too.
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def prompt(
        self, prompt: list[Any], session_id: str, message_id: str | None = None, **kwargs: Any
    ) -> PromptResponse:
        text = "\n".join(_block_text(block) for block in prompt)
        if "HANG" in text:
            await asyncio.sleep(30)
        if "REFUSE" in text:
            return PromptResponse(stop_reason="refusal")
        if "EMPTY" in text:
            return PromptResponse(stop_reason="end_turn")
        sleep_for = _sleep_seconds(text)
        if sleep_for is not None:
            # Stream a partial answer BEFORE the long sleep, so a panel deadline that cuts this voice has a
            # harvestable partial. The cut cancels the turn during the sleep, before the final message below.
            await self._client.session_update(session_id, update_agent_message_text("partial-so-far"))
            await asyncio.sleep(sleep_for)
        answer = _planted_answer(text)
        if answer is None:
            answer = "42" if "17 + 25" in text else f"ECHO:{text[:40]}"
        await self._client.session_update(session_id, update_agent_thought_text("thinking"))
        await self._client.session_update(session_id, update_agent_message_text(answer))
        return PromptResponse(stop_reason="end_turn")


async def _main() -> None:
    await run_agent(FakeAgent())  # type: ignore[arg-type]  # a partial Agent: only the methods tests drive


if __name__ == "__main__":
    asyncio.run(_main())
