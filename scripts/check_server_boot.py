# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Boot the MCP server for real and speak JSON-RPC to it.

``python -m rutherford --smoke`` builds the app and returns BEFORE ``mcp.run``, so it proves config
loading and registry construction and nothing about the server. Every failure that lives in the
transport is invisible to it: a ``mcp.run`` keyword a FastMCP release renamed, a dependency installing
a stdout writer, a tool that fails to register. Those land at boot, on every user, with a green build.

This starts the process, completes ``initialize``, lists the tools, and calls one. It also asserts the
thing no unit test can: that NOTHING but JSON-RPC reaches stdout. The server speaks MCP on stdout while
its own logs and every spawned agent's stderr go elsewhere, so a single stray ``print`` corrupts the
protocol for every client.

Two modes, and the second is the one that covers users:

* default -- drive ``python -m rutherford`` from the current environment. This is the LOCKED set, the
  same one ``uv sync --locked`` gives CI.
* ``--wheel`` -- build the distribution and run it through ``uvx --refresh``, which resolves
  dependencies fresh from the index with no lock. That is what a ``uvx rutherford-mcp-server`` user
  actually gets, and it is a different dependency set: measured 2026-09-05, the lock held fastmcp 3.3.1
  while a fresh resolve took 4.0.3. Without this mode nothing in the repository ever executes the
  versions that ship.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Every tool the server must register. A missing one means a registration failure that imports alone
#: would not reveal; a NEW one that is not here is a deliberate change and should be added.
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
_BOOT_TIMEOUT_S = 90.0


class BootFailure(RuntimeError):
    """The server did not boot, did not answer, or wrote something other than JSON-RPC to stdout."""


def _read_message(proc: subprocess.Popen[str], deadline: float, stray: list[str]) -> dict[str, object]:
    """Read one JSON-RPC object from stdout, recording any line that is not one.

    A non-JSON line is collected rather than raised on immediately: the point of this check is to report
    everything polluting the channel, not just the first thing.
    """
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout is not None else ""
        if not line:
            raise BootFailure("the server closed stdout before answering")
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            stray.append(text[:200])
            continue
        if isinstance(parsed, dict):
            return parsed
        stray.append(text[:200])
    raise BootFailure(f"no answer within {_BOOT_TIMEOUT_S}s")


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
    )
    stray: list[str] = []
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    try:
        if proc.stdin is None:  # pragma: no cover - Popen always gives a pipe here
            raise BootFailure("no stdin pipe")

        def send(payload: dict[str, object]) -> None:
            proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]

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
        info = _read_message(proc, deadline, stray).get("result", {})
        server = info.get("serverInfo", {}) if isinstance(info, dict) else {}
        name = server.get("name") if isinstance(server, dict) else None
        reported = server.get("version") if isinstance(server, dict) else None
        print(f"    serverInfo: name={name!r} version={reported!r}")
        if name != "rutherford":
            raise BootFailure(f"serverInfo.name is {name!r}, expected 'rutherford'")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = _read_message(proc, deadline, stray).get("result", {})
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        print(f"    tools: {len(names)}")
        missing = EXPECTED_TOOLS - names
        if missing:
            raise BootFailure(f"tools did not register: {sorted(missing)}")

        # One real call, so registration is proven to be more than a name in a list.
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "capabilities", "arguments": {}}})
        called = _read_message(proc, deadline, stray).get("result", {})
        content = called.get("content", []) if isinstance(called, dict) else []
        if not content:
            raise BootFailure("the capabilities tool returned no content")
        print(f"    capabilities call: {len(str(content))} chars")
    finally:
        if proc.stdin is not None:
            with contextlib.suppress(OSError):  # pragma: no cover - the server may already be gone
                proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged server
            proc.kill()

    if stray:
        for line in stray[:5]:
            print(f"    STRAY STDOUT: {line}")
        raise BootFailure(f"{len(stray)} non-JSON-RPC line(s) on stdout -- this corrupts MCP for every client")
    print("    stdout carried JSON-RPC only")


def _wheel_argv() -> list[str]:
    """Build the distribution and return an argv that runs it through a FRESH, UNLOCKED resolve."""
    subprocess.run(["uv", "build", "--wheel"], cwd=REPO_ROOT, check=True, capture_output=True)  # noqa: S607
    wheels = sorted((REPO_ROOT / "dist").glob("rutherford_mcp_server-*-py3-none-any.whl"))
    if not wheels:
        raise BootFailure("uv build produced no wheel")
    return ["uvx", "--refresh", "--from", str(wheels[-1]), "rutherford-mcp-server"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
