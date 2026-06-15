# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Rutherford's side of a live ACP session: the ``Client`` callbacks an agent invokes.

``session_update`` folds the agent's streaming notifications into the :class:`EventJournal`;
``request_permission`` and the ``fs``/``terminal`` callbacks are answered by the :class:`PermissionPolicy`.
Read-only is the default posture: writes and terminals are denied (a :class:`acp.RequestError`) unless the
policy allows them. Every decision is journaled, so the turn record shows what the agent was allowed to do.

Two safety substrates live here, active only when a ``sandbox_root`` is bound (a mutating delegation runs
the agent inside an isolated worktree / temp copy -- see :mod:`rutherford.acp.sandbox`):

* **FileGateway** -- confines ``write_text_file`` (always) and ``read_text_file`` (in a mutating sandbox) to
  the sandbox root. A path that escapes the root (``..``, an absolute path outside, a symlink that resolves
  out) is rejected with a clean :class:`acp.RequestError` and journaled, so a sandboxed agent cannot reach
  the rest of the disk through the ACP file callbacks.
* **TerminalBroker** -- implements the ACP terminal callbacks for ``write`` / ``yolo``: it spawns the command
  with its cwd confined to the sandbox root, captures bounded stdout/stderr, and reaps the process tree on
  kill / release. ``read_only`` / ``propose`` keep denying terminal execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
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
    TerminalExitStatus,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from .journal import EventJournal, JournalEvent
from .permission import PermissionPolicy
from .teardown import reap, snapshot_descendants

#: JSON-RPC error code Rutherford returns when it declines an agent callback (an internal-error code; the
#: protocol has no dedicated "permission denied" callback code, so the message carries the reason).
_DECLINED = -32603
#: JSON-RPC "method not found", for an unknown extension method.
_METHOD_NOT_FOUND = -32601

#: How long a brokered terminal command may run before it is killed (its tree reaped) and reported as a
#: timeout exit. Bounds a runaway build/test the agent kicks off inside the sandbox.
_TERMINAL_TIMEOUT_S = 120.0
#: Max bytes of combined stdout/stderr a brokered terminal keeps; output past it is truncated (the response
#: reports ``truncated=True``). Bounds memory against a command that floods output.
_TERMINAL_OUTPUT_CAP = 1 * 1024 * 1024


def _confine(root: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` and assert it stays within ``root`` (the FileGateway path-escape guard).

    Resolves relative paths against ``root`` and follows symlinks (``Path.resolve``), so ``..`` traversal, an
    absolute path outside the root, and a symlink that points out are all caught by the final containment
    check. Raises :class:`acp.RequestError` on an escape -- the caller journals the denial.
    """
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise RequestError(_DECLINED, f"path {raw_path!r} escapes the sandbox root")
    return resolved


class _BrokeredTerminal:
    """One live (or finished) command a :class:`TerminalBroker` is tracking, with bounded captured output."""

    def __init__(self, process: subprocess.Popen[bytes], command: str) -> None:
        self.process = process
        self.command = command
        self.output = bytearray()
        self.truncated = False
        self.exit_code: int | None = None
        self._reader = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Drain the process's combined stdout/stderr into the bounded buffer until it closes."""
        stream = self.process.stdout
        if stream is None:  # pragma: no cover - the broker always wires stdout
            return
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, stream.read, 4096)
            if not chunk:
                break
            room = _TERMINAL_OUTPUT_CAP - len(self.output)
            if room <= 0:
                self.truncated = True
                continue
            if len(chunk) > room:
                self.output.extend(chunk[:room])
                self.truncated = True
            else:
                self.output.extend(chunk)

    def text(self) -> str:
        """The captured output decoded as UTF-8 (replacement on a bad byte), for the ACP response."""
        return self.output.decode("utf-8", errors="replace")

    async def wait(self, timeout_s: float) -> int:
        """Wait for the command to finish (bounded by ``timeout_s``); kill + reap and report -1 on a timeout."""
        loop = asyncio.get_running_loop()
        try:
            code = await asyncio.wait_for(loop.run_in_executor(None, self.process.wait), timeout=timeout_s)
        except TimeoutError:
            await self.kill()
            self.exit_code = -1
            return -1
        await self._reader
        self.exit_code = code
        return code

    async def kill(self) -> None:
        """Terminate the command and reap its descendant tree (best-effort; never raises)."""
        if self.process.poll() is None:
            descendants = await asyncio.to_thread(snapshot_descendants, self.process.pid)
            with contextlib.suppress(OSError):  # pragma: no cover - already dead
                self.process.kill()
            await asyncio.to_thread(reap, descendants)
        if not self._reader.done():
            self._reader.cancel()


class TerminalBroker:
    """Runs agent terminal commands confined to the sandbox root, for ``write`` / ``yolo`` (N1 §5.4).

    Each ``create_terminal`` spawns the command with cwd pinned to the sandbox root and a sanitized argv (no
    shell), captures bounded output, and returns a terminal id. ``terminal_output`` / ``wait_for_terminal_exit``
    read the capture and exit; ``kill_terminal`` / ``release_terminal`` tear the process tree down. Only ever
    constructed for a mutating sandbox -- ``read_only`` / ``propose`` keep denying terminal at the client.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._terminals: dict[str, _BrokeredTerminal] = {}
        self._counter = 0

    async def create(self, command: str, args: list[str] | None, env: list[EnvVariable] | None) -> str:
        """Spawn ``command`` with cwd confined to the root and a sanitized environment; return its id."""
        argv = [command, *(args or [])]
        child_env = dict(os.environ)
        for var in env or []:
            child_env[var.name] = var.value
        try:
            process = subprocess.Popen(  # noqa: ASYNC220 - long-lived process drained off-thread, not awaited
                argv,
                cwd=str(self._root),
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RequestError(_DECLINED, f"could not run {command!r}: {exc}") from exc
        self._counter += 1
        term_id = f"term-{self._counter}"
        self._terminals[term_id] = _BrokeredTerminal(process, command)
        return term_id

    def output(self, terminal_id: str) -> TerminalOutputResponse:
        """The captured-so-far output of a terminal (with a finished-exit status when it has exited)."""
        term = self._get(terminal_id)
        status = TerminalExitStatus(exit_code=term.exit_code) if term.exit_code is not None else None
        return TerminalOutputResponse(output=term.text(), truncated=term.truncated, exit_status=status)

    async def wait_exit(self, terminal_id: str) -> WaitForTerminalExitResponse:
        """Block until the terminal exits (bounded) and report its exit code."""
        term = self._get(terminal_id)
        code = await term.wait(_TERMINAL_TIMEOUT_S)
        return WaitForTerminalExitResponse(exit_code=code)

    async def kill(self, terminal_id: str) -> None:
        """Kill the terminal's process tree (leaving it tracked so output can still be read)."""
        await self._get(terminal_id).kill()

    async def release(self, terminal_id: str) -> None:
        """Kill and forget the terminal, freeing its tracking slot."""
        term = self._terminals.pop(terminal_id, None)
        if term is not None:
            await term.kill()

    async def shutdown(self) -> None:
        """Kill and forget every tracked terminal (called when the session closes). Best-effort."""
        for term in list(self._terminals.values()):
            await term.kill()
        self._terminals.clear()

    def _get(self, terminal_id: str) -> _BrokeredTerminal:
        term = self._terminals.get(terminal_id)
        if term is None:
            raise RequestError(_DECLINED, f"unknown terminal id {terminal_id!r}")
        return term


class RutherfordACPClient:
    """The ``Client`` half of one Rutherford ACP session: journal the stream, enforce the policy.

    When ``sandbox_root`` is set (a mutating delegation running inside an isolated worktree / temp copy), the
    FileGateway confines the agent's file callbacks to that root and the TerminalBroker runs its commands
    there. With no sandbox root the legacy behaviour holds: reads are served from anywhere, writes are gated
    only by the mode, and terminal is denied.
    """

    def __init__(
        self, *, journal: EventJournal, policy: PermissionPolicy, cwd: str, sandbox_root: str | None = None
    ) -> None:
        self.journal = journal
        self.policy = policy
        self.cwd = cwd
        #: The confinement root for the FileGateway / TerminalBroker, or ``None`` when this session is not
        #: sandboxed (a read_only / propose run, or a write run whose working_dir could not be sandboxed).
        self._sandbox_root = Path(sandbox_root).resolve() if sandbox_root is not None else None
        #: The terminal broker, built lazily on the first ``create_terminal`` in a writable sandbox.
        self._broker: TerminalBroker | None = None
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
        """Serve a file read (always permitted), optionally windowed by 1-based ``line`` and ``limit``.

        In a mutating sandbox the read is confined to the sandbox root (a sandboxed agent should not read
        arbitrary disk); a path that escapes the root is rejected and journaled. Without a sandbox, reads are
        served from anywhere (the answer needs to see the user's code).
        """
        if not self.policy.allow_fs_read:  # pragma: no cover - read is always permitted today
            raise RequestError(_DECLINED, "file reads are not permitted")
        target = path
        if self._sandbox_root is not None:
            try:
                target = str(_confine(self._sandbox_root, path))
            except RequestError:
                self.journal.append(JournalEvent(kind="fs_read_denied", detail=path))
                raise
        try:
            text = await asyncio.to_thread(Path(target).read_text, encoding="utf-8")
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
        """Apply a file write when the policy allows it, else decline and journal the denial.

        A mutating write is confined to the sandbox root by the FileGateway: a path that escapes the root
        (``..``, an absolute path outside, a symlink out) is rejected with a clean error and journaled, so the
        agent cannot write outside its isolated worktree / copy. The write lands in the sandbox; the
        delegation service diffs/applies it back afterwards.
        """
        if not self.policy.allow_writes:
            self.journal.append(JournalEvent(kind="fs_write_denied", detail=path))
            raise RequestError(_DECLINED, f"writes are not permitted in {self.policy.mode.value} mode")
        target = path
        if self._sandbox_root is not None:
            try:
                target = str(_confine(self._sandbox_root, path))
            except RequestError:
                self.journal.append(JournalEvent(kind="fs_write_denied", detail=path))
                raise

        def _write() -> None:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
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
        """Spawn a terminal command confined to the sandbox root (write/yolo only), else decline.

        ``read_only`` / ``propose`` keep denying terminal execution (the agent's commands are write-capable by
        default, and propose is "show the diff", not "run things"). A ``write`` / ``yolo`` sandbox runs the
        command via the :class:`TerminalBroker` with its cwd pinned to the sandbox root, regardless of the
        ``cwd`` the agent asked for (confinement wins over the request).
        """
        if not (self.policy.allow_terminal and self._sandbox_root is not None):
            self.journal.append(JournalEvent(kind="terminal_denied", detail=command))
            raise RequestError(_DECLINED, "terminal execution is not permitted in this mode")
        if self._broker is None:
            self._broker = TerminalBroker(self._sandbox_root)
        term_id = await self._broker.create(command, args, env)
        self.journal.append(JournalEvent(kind="terminal", detail=command))
        return CreateTerminalResponse(terminal_id=term_id)

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        """Return a brokered terminal's captured output (and exit status when it has finished)."""
        if self._broker is None:
            raise RequestError(_DECLINED, "terminal execution is not permitted in this mode")
        return self._broker.output(terminal_id)

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        """Block until a brokered terminal exits (bounded) and return its exit code."""
        if self._broker is None:
            raise RequestError(_DECLINED, "terminal execution is not permitted in this mode")
        return await self._broker.wait_exit(terminal_id)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse | None:
        """Kill a brokered terminal's process tree (its output stays readable)."""
        if self._broker is None:
            raise RequestError(_DECLINED, "terminal execution is not permitted in this mode")
        await self._broker.kill(terminal_id)
        return None

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        """Kill and forget a brokered terminal, freeing its tracking slot."""
        if self._broker is None:
            raise RequestError(_DECLINED, "terminal execution is not permitted in this mode")
        await self._broker.release(terminal_id)
        return None

    async def shutdown_terminals(self) -> None:
        """Tear down every brokered terminal (called by the session on close). Best-effort; never raises."""
        if self._broker is not None:
            await self._broker.shutdown()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError(_METHOD_NOT_FOUND, f"unsupported ext method: {method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None
