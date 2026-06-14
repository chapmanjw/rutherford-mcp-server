# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Rutherford's side of a live ACP session: the ``Client`` callbacks an agent invokes.

``session_update`` folds the agent's streaming notifications into the :class:`EventJournal`;
``request_permission`` and the ``fs``/``terminal`` callbacks are answered by the :class:`PermissionPolicy`.
Read-only is the default posture: writes and terminals are denied (a :class:`acp.RequestError`) unless the
policy allows them. Every decision is journaled, so the turn record shows what the agent was allowed to do.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from acp import RequestError
from acp.schema import (
    AllowedOutcome,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from .journal import EventJournal, JournalEvent
from .permission import PermissionPolicy

#: JSON-RPC error code Rutherford returns when it declines an agent callback (an internal-error code; the
#: protocol has no dedicated "permission denied" callback code, so the message carries the reason).
_DECLINED = -32603
#: JSON-RPC "method not found", for an unknown extension method.
_METHOD_NOT_FOUND = -32601


class RutherfordACPClient:
    """The ``Client`` half of one Rutherford ACP session: journal the stream, enforce the policy."""

    def __init__(self, *, journal: EventJournal, policy: PermissionPolicy, cwd: str) -> None:
        self.journal = journal
        self.policy = policy
        self.cwd = cwd
        self._agent: Any = None

    def on_connect(self, conn: Any) -> None:
        """Receive the agent-facing connection handle the SDK hands back on connect."""
        self._agent = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """The protocol-required notification sink: a no-op.

        The ``session/update`` stream is journaled by a synchronous stream observer (see
        :func:`rutherford.acp.session.run_acp_turn`), which records updates inline in the read loop so the
        journal is complete the moment the prompt response resolves. This async handler runs on a separate
        dispatcher task that races that response, so it deliberately does NOT journal.
        """
        return None

    async def request_permission(
        self, options: list[PermissionOption], session_id: str, tool_call: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        """Grant or decline a tool-call permission request per the policy."""
        option_id = self.policy.select_permission(options)
        self.journal.append(
            JournalEvent(kind="permission_request", status="allowed" if option_id is not None else "denied")
        )
        if option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option_id))

    async def read_text_file(
        self, path: str, session_id: str, limit: int | None = None, line: int | None = None, **kwargs: Any
    ) -> ReadTextFileResponse:
        """Serve a file read (always permitted), optionally windowed by 1-based ``line`` and ``limit``."""
        if not self.policy.allow_fs_read:  # pragma: no cover - read is always permitted today
            raise RequestError(_DECLINED, "file reads are not permitted")
        try:
            text = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
        except OSError as exc:
            raise RequestError(_DECLINED, f"could not read {path}: {exc}") from exc
        if line is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, (line or 1) - 1)
            end = start + limit if limit is not None else len(lines)
            text = "".join(lines[start:end])
        self.journal.append(JournalEvent(kind="fs_read", detail=path))
        return ReadTextFileResponse(content=text)

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        """Apply a file write when the policy allows it, else decline and journal the denial."""
        if not self.policy.allow_writes:
            self.journal.append(JournalEvent(kind="fs_write_denied", detail=path))
            raise RequestError(_DECLINED, f"writes are not permitted in {self.policy.mode.value} mode")
        await asyncio.to_thread(Path(path).write_text, content, encoding="utf-8")
        self.journal.append(JournalEvent(kind="fs_write", detail=path))
        return None

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        """Decline terminal creation; terminal execution is not wired in this build."""
        self.journal.append(JournalEvent(kind="terminal_denied", detail=command))
        raise RequestError(_DECLINED, "terminal execution is not supported yet")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        raise RequestError(_DECLINED, "terminal execution is not supported yet")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError(_DECLINED, "terminal execution is not supported yet")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse | None:
        raise RequestError(_DECLINED, "terminal execution is not supported yet")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise RequestError(_DECLINED, "terminal execution is not supported yet")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError(_METHOD_NOT_FOUND, f"unsupported ext method: {method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None
