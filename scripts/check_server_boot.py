# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Boot the MCP server for real and speak JSON-RPC to it.

``python -m rutherford --smoke`` builds the app and returns BEFORE ``mcp.run``, so it proves config
loading and registry construction and nothing about the server. Every failure that lives in the
transport is invisible to it: a ``mcp.run`` keyword a FastMCP release renamed, a dependency installing
a stdout writer, a tool that registers but raises. Those land at boot, on every user, with a green
build.

This starts the process and completes a real exchange: ``initialize``, ``tools/list``, one
``tools/call``. What it asserts, and why each one is here rather than in a unit test:

* ``serverInfo.version`` equals this distribution's version, read off the WIRE. FastMCP fills that
  field with its own version when the argument is omitted, which is how a client connecting to 3.2.0
  came to be shown ``3.3.1``. A unit test on the helper cannot catch that -- it would pass while the
  value never reached the field.
* Every registered tool is present, and a real call returns a NON-ERROR result. ``isError`` is checked
  explicitly, because a failing tool answers with content like any other.
* Nothing but JSON-RPC reaches stdout. The server speaks MCP there, so one stray write corrupts the
  protocol for every client. The child runs UNBUFFERED for this: a piped stdout is block-buffered, so a
  stray write otherwise sits in the child's buffer and is discarded on teardown, and the check reports
  a clean channel for a server that pollutes it.

Both streams are read on threads. A blocking ``readline`` against a live but silent server never
returns, so a deadline checked between lines never fires -- the "no answer" failure this exists to
produce would become an indefinite hang. Draining stderr also keeps a chatty child from filling that
pipe and blocking mid-exchange.

Two modes, and the second is the one that covers users:

* default -- drive ``python -m rutherford`` from the current environment, the LOCKED set CI installs.
* ``--wheel`` -- build the distribution and run it through ``uvx --refresh``, resolving dependencies
  fresh from the index with no lock. That is what a ``uvx rutherford-mcp-server`` user gets, and it is
  a different set: measured 2026-09-05, the lock held fastmcp 3.3.1 while a fresh resolve took 4.0.3.
  Without this mode nothing in the repository ever executes the versions that ship.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import IO, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Every tool the server must register. Pinned against live registration by tests/test_gate.py, so this
#: list cannot quietly cover less than it claims.
EXPECTED_TOOLS = frozenset(
    {
        "activity",
        "analyze",
        "cancel_job",
        "capabilities",
        "consensus",
        "continue_job",
        "debate",
        "delegate",
        "discover",
        "doctor",
        "job_result",
        "job_status",
        "list_jobs",
        "list_roles",
        "plan",
        "reload_panels",
        "review",
        "setup",
    }
)
_RESPONSE_TIMEOUT_S = 60.0
_EXIT_TIMEOUT_S = 15.0
#: Lines of the child's stderr shown when the check fails. Drained continuously either way.
_STDERR_TAIL_LINES = 15


class BootFailure(RuntimeError):
    """The server did not boot, did not answer correctly, or wrote something other than JSON-RPC."""


class _Reader:
    """Drains one stream on a thread, so a silent server is a timeout rather than a hang."""

    def __init__(self, stream: IO[str] | None) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.collected: list[str] = []
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: IO[str] | None) -> None:
        try:
            if stream is not None:
                for line in stream:
                    self.collected.append(line)
                    self.lines.put(line)
        except (OSError, ValueError):  # pragma: no cover - the pipe closed under us
            pass
        finally:
            self.lines.put(None)

    def next_line(self, timeout: float) -> str | None:
        """The next line, or ``None`` at EOF; a timeout is a failure, not a wait."""
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            raise BootFailure(f"the server sent nothing for {timeout:.0f}s (it booted but never answered)") from None

    def settle(self, timeout: float = 1.0) -> None:
        """Let the stream finish, so writes made during shutdown are still counted."""
        self._thread.join(timeout=timeout)


def _is_response_to(message: dict[str, Any], request_id: int) -> bool:
    """Whether ``message`` is the JSON-RPC response to ``request_id``.

    Checked rather than assumed: without it, ANY JSON object on the channel is accepted as the answer,
    which is exactly the pollution this check exists to detect.
    """
    return (
        message.get("jsonrpc") == "2.0"
        and message.get("id") == request_id
        and ("result" in message or "error" in message)
    )


def _await_response(reader: _Reader, request_id: int, stray: list[str]) -> dict[str, Any]:
    """Read until the response to ``request_id`` arrives; record anything else on stdout as stray."""
    deadline = time.monotonic() + _RESPONSE_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BootFailure(f"no response to request {request_id} within {_RESPONSE_TIMEOUT_S:.0f}s")
        line = reader.next_line(remaining)
        if line is None:
            raise BootFailure(f"the server closed stdout before answering request {request_id}")
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            stray.append(text[:200])
            continue
        if not isinstance(message, dict):
            stray.append(text[:200])
        elif _is_response_to(message, request_id):
            if "error" in message:
                raise BootFailure(f"request {request_id} failed: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        elif "method" not in message:
            # A notification is legitimate traffic. Anything else is not ours.
            stray.append(text[:200])


def _check_version(result: dict[str, Any]) -> None:
    """Assert the version the client is TOLD matches this distribution's."""
    server = result.get("serverInfo") or {}
    server = server if isinstance(server, dict) else {}
    name, reported = server.get("name"), server.get("version")
    print(f"    serverInfo: name={name!r} version={reported!r}")
    if name != "rutherford":
        raise BootFailure(f"serverInfo.name is {name!r}, expected 'rutherford'")
    try:
        expected = dist_version("rutherford-mcp-server")
    except PackageNotFoundError:  # pragma: no cover - an uninstalled source checkout
        print("    (package metadata absent; version not asserted)")
        return
    if reported != expected:
        raise BootFailure(
            f"serverInfo.version is {reported!r} but this distribution is {expected!r} -- "
            "FastMCP reports its OWN version when the `version=` argument is omitted"
        )


def _check_tools(result: dict[str, Any]) -> None:
    tools = result.get("tools") or []
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    print(f"    tools: {len(names)}")
    missing = EXPECTED_TOOLS - names
    if missing:
        raise BootFailure(f"tools did not register: {sorted(missing)}")


def _check_call(result: dict[str, Any]) -> None:
    """A tool must answer, and must not answer with a failure.

    ``isError`` is the part worth stating: a tool that raises still returns content, so a check asking
    only for non-empty content would pass on a server whose every call fails.
    """
    if result.get("isError"):
        raise BootFailure(f"the capabilities tool returned isError: {str(result.get('content'))[:200]}")
    if not result.get("content"):
        raise BootFailure("the capabilities tool returned no content")
    print(f"    capabilities call: {len(str(result.get('content')))} chars")


def _exchange(proc: subprocess.Popen[str], out: _Reader, stray: list[str]) -> None:
    """Run the protocol exchange, asserting each response."""

    def send(payload: dict[str, Any]) -> None:
        if proc.stdin is None:  # pragma: no cover - Popen was given a pipe
            raise BootFailure("no stdin pipe")
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "check_server_boot", "version": "1"},
            },
        }
    )
    _check_version(_await_response(out, 1, stray))
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    _check_tools(_await_response(out, 2, stray))
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "capabilities", "arguments": {}}})
    _check_call(_await_response(out, 3, stray))


def _collect_stray(lines: list[str], stray: list[str]) -> None:
    """Record every stdout line that is not a JSON-RPC message, including any written during shutdown."""
    for line in lines:
        text = line.strip()
        if not text or text[:200] in stray:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            stray.append(text[:200])
            continue
        if not isinstance(parsed, dict) or parsed.get("jsonrpc") != "2.0":
            stray.append(text[:200])


def _shutdown(proc: subprocess.Popen[str], out: _Reader, err: _Reader) -> None:
    """Close stdin, wait for exit, and let both readers finish."""
    if proc.stdin is not None:
        with contextlib.suppress(OSError):  # pragma: no cover - the child may already be gone
            proc.stdin.close()
    try:
        proc.wait(timeout=_EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_EXIT_TIMEOUT_S)
    out.settle()
    err.settle()


def _drive(argv: list[str], label: str) -> None:
    """Run one server process end to end and assert the protocol invariants."""
    print(f"=== {label} ===")
    print(f"    argv: {' '.join(argv[:3])}{' ...' if len(argv) > 3 else ''}")
    proc = subprocess.Popen(  # noqa: S603 - argv is built here, never from user input
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    out, err = _Reader(proc.stdout), _Reader(proc.stderr)
    stray: list[str] = []
    try:
        _exchange(proc, out, stray)
    except BootFailure:
        tail = "".join(err.collected).splitlines()[-_STDERR_TAIL_LINES:]
        if tail:
            print("    --- child stderr (tail) ---")
            for line in tail:
                print(f"    {line}")
        raise
    finally:
        _shutdown(proc, out, err)

    _collect_stray(out.collected, stray)
    if stray:
        for line in stray[:5]:
            print(f"    STRAY STDOUT: {line}")
        raise BootFailure(f"{len(stray)} non-JSON-RPC line(s) on stdout -- this corrupts MCP for every client")
    print("    stdout carried JSON-RPC only")


def _wheel_argv() -> list[str]:
    """Build the distribution and return an argv running it through a FRESH, UNLOCKED resolve.

    The wheel is located by THIS version rather than by taking the newest file in ``dist/``: that
    directory accumulates artifacts, so a lexicographic pick can silently run a stale build.
    """
    subprocess.run(["uv", "build", "--wheel"], cwd=REPO_ROOT, check=True, capture_output=True)  # noqa: S607
    try:
        expected = dist_version("rutherford-mcp-server")
    except PackageNotFoundError:  # pragma: no cover - an uninstalled source checkout
        expected = "*"
    matches = sorted((REPO_ROOT / "dist").glob(f"rutherford_mcp_server-{expected}-py3-none-any.whl"))
    if not matches:
        raise BootFailure(f"uv build produced no wheel for version {expected}")
    return ["uvx", "--refresh", "--from", str(matches[-1]), "rutherford-mcp-server"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Boot the MCP server and speak JSON-RPC to it.")
    parser.add_argument(
        "--wheel",
        action="store_true",
        help="build the wheel and drive it through `uvx --refresh` (a fresh, UNLOCKED dependency resolve)",
    )
    args = parser.parse_args()
    try:
        if args.wheel:
            _drive(_wheel_argv(), "built wheel, dependencies resolved fresh (what a uvx user gets)")
        else:
            _drive([sys.executable, "-m", "rutherford"], "current environment (the locked set CI installs)")
    except BootFailure as exc:
        print(f"server boot check FAILED: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:  # pragma: no cover - build failure
        print(f"server boot check FAILED: build error: {exc}", file=sys.stderr)
        return 1
    print("server boot ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
