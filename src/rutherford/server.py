# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The FastMCP server (ACP-native): a thin stdio transport over the ACP orchestration core.

Each tool validates input, calls a tool function, and returns the TOON-encoded envelope, mapping a
:class:`~rutherford.domain.errors.RutherfordError` to an MCP tool error. All orchestration lives in the
services and the ACP runtime, so this layer stays a thin wrapper -- the "good duck": still an MCP server
with the same tool surface, now driving agents over ACP. (v3 rebuild: ``delegate`` and ``capabilities``
land first; ``consensus`` / ``debate`` and the rest are re-added over the same core.)
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .context import AppContext, build_app_context, error_payload_from, tool_error
from .domain.error_codes import ErrorCode
from .domain.errors import ConfigError, RutherfordError
from .runtime.logging import configure_logging
from .tools.capabilities import capabilities_tool, doctor_tool
from .tools.consensus import consensus_tool
from .tools.debate import debate_tool
from .tools.delegate import delegate_tool
from .tools.jobs import activity_tool, cancel_job_tool, job_result_tool, job_status_tool, list_jobs_tool
from .tools.roles import list_roles_tool
from .tools.setup import setup_tool

mcp: FastMCP = FastMCP(
    "rutherford",
    instructions=(
        "Rutherford orchestrates other agentic coding agents over the Agent Client Protocol (ACP). Use "
        "`delegate` to hand a task to one agent and `capabilities` to see which agents are available. "
        "Delegations default to the configured default_safety_mode (read_only out of the box); write and "
        "yolo are explicit opt-in behind a trusted workspace. Long tasks can run as background jobs "
        "(mode=async), enumerated with `list_jobs`, polled with `job_status` / `job_result`, and cancelled "
        "with `cancel_job`; `activity` is the focused snapshot of just the jobs in flight right now. First "
        "time here? `setup` shows where config lives and scaffolds a starter config.toml."
    ),
)

_APP: AppContext | None = None


def get_app() -> AppContext:
    """Return the shared application context, building it lazily on first use (and configuring logging)."""
    global _APP
    if _APP is None:
        _APP = build_app_context()
        configure_logging(_APP.config.log_level, _APP.config.log_format)
    return _APP


async def _guarded(coro: Awaitable[str]) -> str:
    """Await a tool body, mapping Rutherford and unexpected errors to MCP tool errors.

    A :class:`RutherfordError` is structured and client-safe, so its payload passes through. An unexpected
    exception gets a fixed client message while the full traceback goes to the server-side log.
    """
    try:
        return await coro
    except RutherfordError as exc:
        raise ToolError(error_payload_from(exc)) from exc
    except Exception as exc:
        logging.getLogger("rutherford.server").exception("unexpected error in a tool call")
        raise ToolError(tool_error(ErrorCode.INTERNAL, "internal server error; see the server log")) from exc


@mcp.tool
async def delegate(
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    trust_workspace: bool = False,
    role: str | None = None,
    mode: str = "sync",
) -> str:
    """Delegate a task to one ACP agent and return its normalized result.

    `cli` is an agent id (see `capabilities`); `model` is optional (the agent's default otherwise).
    `safety_mode` is read_only | propose | write | yolo; when omitted, the configured `default_safety_mode`
    applies (read_only out of the box). write and yolo also need a trusted workspace (`trust_workspace=true`
    or a configured allowlist). `files` lists paths to put in scope. `role` names a persona (see
    `list_roles`) whose system prompt is prepended to `prompt`. `mode="async"` runs the turn as a
    background job and returns a `job_id` (poll with `job_status` / `job_result`); `mode="sync"` awaits it.
    """
    return await _guarded(
        delegate_tool(
            get_app(),
            cli=cli,
            prompt=prompt,
            model=model,
            working_dir=working_dir,
            files=files,
            safety_mode=safety_mode,
            timeout_s=timeout_s,
            trust_workspace=trust_workspace,
            role=role,
            mode=mode,
        )
    )


@mcp.tool
async def consensus(
    prompt: str,
    targets: list[Any] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    role: str | None = None,
    mode: str = "sync",
) -> str:
    """Ask the same prompt of several ACP agents in parallel and return every voice.

    `targets` is a list of `{cli, model}` objects or `cli` / `cli:model` strings; each runs as its own ACP
    session concurrently. `safety_mode` and `timeout_s` apply to every voice; one failing voice is returned
    as a failed result, never an aborted panel. `role` names a persona (see `list_roles`) prepended to the
    prompt every voice receives. `mode="async"` runs the panel as a background job and returns a `job_id`
    (poll with `job_status` / `job_result`); `mode="sync"` awaits it.
    """
    return await _guarded(
        consensus_tool(
            get_app(),
            prompt=prompt,
            targets=targets,
            working_dir=working_dir,
            files=files,
            safety_mode=safety_mode,
            timeout_s=timeout_s,
            role=role,
            mode=mode,
        )
    )


@mcp.tool
async def debate(
    prompt: str,
    targets: list[Any] | None = None,
    rounds: int = 2,
    judge: Any | None = None,
    working_dir: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool = True,
    timeout_s: float | None = None,
    role: str | None = None,
    mode: str = "sync",
) -> str:
    """Have several ACP agents argue a question across rounds and return the full transcript.

    `targets` is a list of `{cli, model}` objects or `cli` / `cli:model` strings; a debate needs at least
    two. Each voice keeps ONE persistent ACP session across all `rounds`: round one is each voice's
    independent answer, and each later round shows a voice the others' latest positions and asks it to
    revise -- the agent remembers its own prior reasoning in-session, so only the delta is sent.
    `synthesize=true` (default) adds a closing summary; `judge` names a target to write it. `role` names a
    persona (see `list_roles`) prepended to the opening prompt every voice argues from. `mode="async"`
    runs the debate as a background job and returns a `job_id` (poll with `job_status` / `job_result`);
    `mode="sync"` awaits it.
    """
    return await _guarded(
        debate_tool(
            get_app(),
            prompt=prompt,
            targets=targets,
            rounds=rounds,
            judge=judge,
            working_dir=working_dir,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
            role=role,
            mode=mode,
        )
    )


@mcp.tool
async def capabilities() -> str:
    """List the ACP agents Rutherford can drive (id, display name, launch command, provider)."""
    return await _guarded(capabilities_tool(get_app()))


@mcp.tool
async def list_roles() -> str:
    """List the available role personas (id, name, description) for the `role` param.

    A role is a reusable system prompt; pass its `id` as `role="<id>"` to `delegate` / `consensus` /
    `debate` and the persona is prepended to your prompt. Built-in roles ship with Rutherford; a
    `role_dirs` directory can add or override one.
    """
    return await _guarded(list_roles_tool(get_app()))


@mcp.tool
async def setup(scope: str = "project", write: bool = False, trust_workspace: bool = False) -> str:
    """Show where config lives and scaffold a starter `config.toml`; the first-run helper.

    `scope` is `project` (`<cwd>/.rutherford/config.toml`) or `global` (the platform config dir's
    `config.toml`). It returns the proposed starter `content` (the most useful settings at their effective
    defaults) and the resolved `path`, plus a snapshot of the agents you already have. Pass `write=true` to
    create the file -- it never overwrites an existing one (`already_exists=true`, `written=false`).
    `trust_workspace=true` adds the current directory to `trusted_workspaces` so write/yolo delegations are
    permitted there.
    """
    return await _guarded(setup_tool(get_app(), scope=scope, write=write, trust_workspace=trust_workspace))


@mcp.tool
async def doctor(agent: str | None = None, timeout_s: float = 60.0) -> str:
    """Probe each agent (or one named `agent`) with a real read-only ACP round trip and report conformance.

    The trustworthy health check for ACP agents: whether each spawns, handshakes, and answers. Each report
    is working / no_answer / handshake_failed / not_installed / error. Slower than `capabilities` (it makes
    a real call per agent); run it to see which of the roster actually drive on this machine.
    """
    return await _guarded(doctor_tool(get_app(), agent=agent, timeout_s=timeout_s))


@mcp.tool
async def list_jobs() -> str:
    """List the background jobs Rutherford is tracking (id, tool, status, summary, timestamps), newest first.

    The light listing -- no heavy result. Fetch a finished job's result with `job_result`. Jobs are
    in-memory: a finished one is evicted after `job_ttl_s`, and a restart clears them all.
    """
    return await _guarded(list_jobs_tool(get_app()))


@mcp.tool
async def activity() -> str:
    """Show the background jobs IN FLIGHT right now (running + pending), each with a live elapsed time.

    The focused "what is happening now" snapshot, distinct from `list_jobs`: where `list_jobs` enumerates
    every tracked job of every status (finished ones included), `activity` returns only the jobs still in
    flight -- `{active: [...], count}` with each row `{job_id, tool, status, summary, started_at,
    elapsed_s}`, longest-running first. Empty (`{active: [], count: 0}`) when nothing is running.
    """
    return await _guarded(activity_tool(get_app()))


@mcp.tool
async def job_status(job_id: str) -> str:
    """Report one background job's status and timings (no heavy result); `JOB_NOT_FOUND` if the id is unknown.

    `status` is pending | running | succeeded | failed | cancelled. Poll this, then call `job_result` once
    the job is `succeeded` (or to read the failure of a `failed` / `cancelled` job).
    """
    return await _guarded(job_status_tool(get_app(), job_id=job_id))


@mcp.tool
async def job_result(job_id: str) -> str:
    """Return a finished background job's result envelope -- identical to the sync tool's envelope.

    A `succeeded` job returns its stored result verbatim; a `failed` job returns its error; a `cancelled`
    or still-running job returns a structured error (poll `job_status` and retry); an unknown id is
    `JOB_NOT_FOUND`.
    """
    return await _guarded(job_result_tool(get_app(), job_id=job_id))


@mcp.tool
async def cancel_job(job_id: str) -> str:
    """Cancel a running background job (killing its work) and return `{job_id, status}`; `JOB_NOT_FOUND` if unknown.

    Cancelling an already-finished job is a no-op that returns its current status. The job's process tree
    is torn down on the next cancellation point.
    """
    return await _guarded(cancel_job_tool(get_app(), job_id=job_id))


def main() -> None:
    """Console entry point: start the stdio MCP server."""
    try:
        global _APP
        _APP = build_app_context()
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    configure_logging(_APP.config.log_level, _APP.config.log_format)
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
