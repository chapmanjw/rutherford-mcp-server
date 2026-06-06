# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The FastMCP server: a thin stdio transport over the orchestration core.

Each tool here is a wrapper that validates input, calls a tool function, and returns the
TOON-encoded envelope, mapping a :class:`~rutherford.domain.errors.RutherfordError` to an MCP tool
error. All orchestration lives in the services and adapters, so the core could be driven by a
different transport without touching it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import __version__
from .context import AppContext, build_app_context, error_payload_from, tool_error
from .domain.error_codes import ErrorCode
from .domain.errors import ConfigError, RutherfordError
from .domain.models import Target
from .services.setup import apply_setup_plan, build_setup_plan, format_plan_summary
from .tools.capabilities import capabilities_tool, doctor_tool
from .tools.consensus import consensus_tool
from .tools.debate import debate_tool
from .tools.delegate import delegate_tool
from .tools.jobs import job_result_tool, job_status_tool
from .tools.panels import reload_panels_tool
from .tools.plan import plan_tool
from .tools.probing import probe_adapter
from .tools.review import review_tool
from .tools.roles import list_roles_tool
from .tools.setup import setup_tool

mcp: FastMCP = FastMCP(
    "rutherford",
    instructions=(
        "Rutherford orchestrates other agentic coding CLIs. Use `delegate` to hand a task to one "
        "CLI, `consensus` to ask several in parallel, `debate` to have several argue across rounds "
        "(returning the full transcript), and `capabilities`/`doctor` to see which are installed and "
        "authenticated. Delegations default to read_only; write and yolo are explicit opt-in. Long "
        "tasks can run as background jobs (mode=async), polled with job_status / job_result."
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
    prompt: str,
    targets: list[Target | str] | str | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    strategy: str | None = None,
    verdict_schema: dict[str, Any] | None = None,
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

    `targets` is a list of `{cli, model}` objects (or `cli` / `cli:model` strings). Omit it, pass an
    empty list, or pass `"all"` to fan out to every installed + authenticated CLI at its default
    model (capped at `max_targets`); the result's `skipped` list explains any adapter left out. Or name
    a saved `panel` (with optional `panel_overrides`) instead of `targets`; the two are mutually
    exclusive. Optional `stances` (parallel to `targets`) steer each voice: for | against | neutral,
    and cannot be combined with the auto-expanded panel. `synthesize=true` adds a server-side combined
    answer (off by default). A `strategy` (`all-voices` | `unanimous` | `majority` | `weighted` |
    `parity-pair`), optionally with a `verdict_schema`, aggregates the voices into an `outcome` instead
    of returning them individually. With `mode="async"` a job id is returned.
    """
    return await _guarded(
        consensus_tool(
            get_app(),
            targets=targets,
            prompt=prompt,
            panel=panel,
            panel_overrides=panel_overrides,
            strategy=strategy,
            verdict_schema=verdict_schema,
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
async def debate(
    prompt: str,
    targets: list[Target | str] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    rounds: int = 2,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str = "read_only",
    synthesize: bool = True,
    timeout_s: float | None = None,
    mode: str = "sync",
    include_raw: bool = False,
) -> str:
    """Have several targets argue a question across rounds and return the full transcript.

    `targets` is a list of `{cli, model}` objects (or `cli` / `cli:model` strings); a debate needs at
    least two. Or name a saved `panel` (with optional `panel_overrides`) instead of `targets`; the two
    are mutually exclusive. `rounds` (default 2) is how many passes the panel makes: round one is each
    voice's independent answer, and each later round shows a voice the others' latest positions and
    asks it to rebut and revise. Optional `stances` (parallel to `targets`) keep a voice arguing for |
    against | neutral throughout. `synthesize=true` (default) adds a closing summary. The result's
    `rounds` hold every voice's answer at every round, so the discussion is fully retraceable. With
    `mode="async"` a job id is returned.
    """
    return await _guarded(
        debate_tool(
            get_app(),
            prompt=prompt,
            targets=list(targets) if targets is not None else None,
            panel=panel,
            panel_overrides=panel_overrides,
            rounds=rounds,
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


@mcp.tool
async def review(
    targets: list[Target] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    paths: list[str] | None = None,
    diff: str | None = None,
    role: str = "codereviewer",
    working_dir: str | None = None,
    safety_mode: str = "read_only",
    synthesize: bool = False,
    timeout_s: float | None = None,
) -> str:
    """Review a diff or a set of files across one or more targets (read-only). Provide diff or paths.

    Name a list of `targets` or a saved `panel` (with optional `panel_overrides`); they are mutually
    exclusive.
    """
    return await _guarded(
        review_tool(
            get_app(),
            targets=list(targets) if targets is not None else None,
            panel=panel,
            panel_overrides=panel_overrides,
            paths=paths,
            diff=diff,
            role=role,
            working_dir=working_dir,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
        )
    )


@mcp.tool
async def plan(
    cli: str,
    goal: str,
    model: str | None = None,
    role: str = "planner",
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str = "read_only",
    timeout_s: float | None = None,
) -> str:
    """Ask one target to produce an ordered implementation plan for a goal (read-only)."""
    return await _guarded(
        plan_tool(
            get_app(),
            cli=cli,
            goal=goal,
            model=model,
            role=role,
            working_dir=working_dir,
            files=files,
            safety_mode=safety_mode,
            timeout_s=timeout_s,
        )
    )


@mcp.tool
async def capabilities() -> str:
    """List every known CLI: whether it is installed, its auth status, and its available models."""
    return await _guarded(capabilities_tool(get_app()))


@mcp.tool
async def doctor(live: bool = True) -> str:
    """Health-probe each adapter (binary, version, auth, runtime) and diagnose unavailable targets.

    Adapters with no non-interactive auth check (e.g. Antigravity) are `unknown` from the cheap
    probe; by default (`live=true`) `doctor` verifies each installed unknown with a minimal real
    round trip -- the only trustworthy signal absent a `whoami`. Pass `live=false` for a
    metadata-only check with no model calls (`capabilities` is the always-cheap snapshot).
    """
    return await _guarded(doctor_tool(get_app(), live=live))


@mcp.tool
async def list_roles() -> str:
    """List the available role personas that can steer a delegation."""
    return await _guarded(list_roles_tool(get_app()))


@mcp.tool
async def reload_panels() -> str:
    """Re-read saved panels from disk (after editing a `panels.toon`) and list those now available."""
    return await _guarded(reload_panels_tool(get_app()))


@mcp.tool
async def setup(
    apply: bool = False,
    force: bool = False,
    safety_mode: str = "read_only",
    trusted_workspaces: list[str] | None = None,
    panel_name: str = "default",
) -> str:
    """Scaffold a starter config and panel from the CLIs you have installed and signed in.

    By default this is a dry run: it returns the proposed files (with their full contents) so you can
    review them. Pass `apply=true` to write them; an existing file is kept unless `force=true`.
    """
    return await _guarded(
        setup_tool(
            get_app(),
            apply=apply,
            force=force,
            safety_mode=safety_mode,
            trusted_workspaces=trusted_workspaces,
            panel_name=panel_name,
        )
    )


def _smoke() -> None:
    """Build the context and print a one-line readiness summary, without starting stdio."""
    try:
        app = build_app_context()
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    ids = ", ".join(app.registry.ids())
    print(f"rutherford-mcp-server {__version__}: ready with {len(app.registry)} adapters: {ids}")


def _init(args: list[str]) -> None:
    """Interactive first-run setup: probe the CLIs, show the plan, and write it on confirmation."""
    assume_yes = "--yes" in args or "-y" in args
    force = "--force" in args
    try:
        app = build_app_context()
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    statuses = [probe_adapter(adapter) for adapter in app.registry.all()]
    plan = build_setup_plan(statuses, env=os.environ)
    print(format_plan_summary(plan))
    if not assume_yes:
        reply = input("\nWrite these files? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted; nothing written.")
            return
    written = apply_setup_plan(plan, force=force)
    if written:
        print("Wrote:\n" + "\n".join(f"  {path}" for path in written))
    else:
        print("Nothing written (files already exist; re-run with --force to overwrite).")


def main() -> None:
    """Console entry point: start the stdio MCP server (or run ``--smoke`` / ``init`` and exit)."""
    argv = sys.argv[1:]
    if "--smoke" in argv:
        _smoke()
        return
    if argv and argv[0] == "init":
        _init(argv[1:])
        return
    try:
        global _APP
        _APP = build_app_context()
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    # Suppress the FastMCP startup banner so a stdio client's stderr log stays clean.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
