# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The FastMCP server: a thin stdio transport over the orchestration core.

Each tool here is a wrapper that validates input, calls a tool function, and returns the
TOON-encoded envelope, mapping a :class:`~rutherford.domain.errors.RutherfordError` to an MCP tool
error. All orchestration lives in the services and adapters, so the core could be driven by a
different transport without touching it.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import __version__
from .context import AppContext, build_app_context, error_payload_from, tool_error
from .domain.error_codes import ErrorCode
from .domain.errors import ConfigError, RutherfordError
from .domain.models import Target
from .tools.consensus import consensus_tool
from .tools.delegate import delegate_tool
from .tools.jobs import job_result_tool, job_status_tool

mcp: FastMCP = FastMCP(
    "rutherford",
    instructions=(
        "Rutherford orchestrates other agentic coding CLIs. Use `delegate` to hand a task to one "
        "CLI, `consensus` to ask several in parallel, and `capabilities`/`doctor` to see which are "
        "installed and authenticated. Delegations default to read_only; write and yolo are explicit "
        "opt-in. Long tasks can run as background jobs (mode=async), polled with job_status / "
        "job_result."
    ),
)

_APP: AppContext | None = None


def get_app() -> AppContext:
    """Return the shared application context, building it lazily on first use."""
    global _APP
    if _APP is None:
        _APP = build_app_context()
    return _APP


async def _guarded(coro: Awaitable[str]) -> str:
    """Await a tool body, mapping Rutherford and unexpected errors to MCP tool errors."""
    try:
        return await coro
    except RutherfordError as exc:
        raise ToolError(error_payload_from(exc)) from exc
    except Exception as exc:  # never let an unexpected error crash the tool call
        raise ToolError(tool_error(ErrorCode.INTERNAL, str(exc))) from exc


@mcp.tool
async def delegate(
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str = "read_only",
    mode: str = "sync",
    timeout_s: float | None = None,
    session_id: str | None = None,
    include_raw: bool = False,
    trust_workspace: bool = False,
) -> str:
    """Delegate a task to one CLI and return its normalized result.

    `cli` is an adapter id (see `capabilities`); `model` is optional (the adapter's default
    otherwise). `safety_mode` is read_only | propose | write | yolo (default read_only); write and
    yolo also need a trusted workspace (`trust_workspace=true` or a configured allowlist). With
    `mode="async"` a job id is returned; poll `job_status` / `job_result`. `session_id` resumes a
    prior session where the CLI supports it.
    """
    return await _guarded(
        delegate_tool(
            get_app(),
            cli=cli,
            prompt=prompt,
            model=model,
            working_dir=working_dir,
            files=files,
            role=role,
            safety_mode=safety_mode,
            mode=mode,
            timeout_s=timeout_s,
            session_id=session_id,
            include_raw=include_raw,
            trust_workspace=trust_workspace,
        )
    )


@mcp.tool
async def consensus(
    targets: list[Target],
    prompt: str,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str = "read_only",
    synthesize: bool = False,
    timeout_s: float | None = None,
    mode: str = "sync",
    include_raw: bool = False,
) -> str:
    """Ask the same prompt of several targets in parallel and return every voice.

    `targets` is a list of `{cli, model}` objects. Optional `stances` (parallel to `targets`) steer
    each voice: for | against | neutral. `synthesize=true` adds a server-side combined answer (off
    by default, so the orchestrator can synthesize the voices itself). With `mode="async"` a job id
    is returned.
    """
    return await _guarded(
        consensus_tool(
            get_app(),
            targets=list(targets),
            prompt=prompt,
            stances=stances,
            working_dir=working_dir,
            files=files,
            role=role,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
            mode=mode,
            include_raw=include_raw,
        )
    )


@mcp.tool
async def job_status(job_id: str) -> str:
    """Return the status and progress of a background job."""
    return await _guarded(job_status_tool(get_app(), job_id=job_id))


@mcp.tool
async def job_result(job_id: str) -> str:
    """Return the result of a finished background job (or a still-running notice)."""
    return await _guarded(job_result_tool(get_app(), job_id=job_id))


def _smoke() -> None:
    """Build the context and print a one-line readiness summary, without starting stdio."""
    try:
        app = build_app_context()
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    ids = ", ".join(app.registry.ids())
    print(f"rutherford-mcp-server {__version__}: ready with {len(app.registry)} adapters: {ids}")


def main() -> None:
    """Console entry point: start the stdio MCP server (or run ``--smoke`` and exit)."""
    if "--smoke" in sys.argv[1:]:
        _smoke()
        return
    try:
        global _APP
        _APP = build_app_context()
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    mcp.run()


if __name__ == "__main__":
    main()
