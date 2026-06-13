# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The FastMCP server: a thin stdio transport over the orchestration core.

Each tool here is a wrapper that validates input, calls a tool function, and returns the
TOON-encoded envelope, mapping a :class:`~rutherford.domain.errors.RutherfordError` to an MCP tool
error. All orchestration lives in the services and adapters, so the core could be driven by a
different transport without touching it.
"""

from __future__ import annotations

import logging
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
from .runtime.logging import configure_logging
from .services.probing import probe_adapter
from .services.setup import apply_setup_plan, build_setup_plan, format_plan_summary
from .tools.capabilities import capabilities_tool, doctor_tool
from .tools.consensus import consensus_tool
from .tools.debate import debate_tool
from .tools.delegate import delegate_tool
from .tools.jobs import cancel_job_tool, job_result_tool, job_status_tool, list_jobs_tool
from .tools.panels import reload_panels_tool
from .tools.plan import plan_tool
from .tools.review import review_tool
from .tools.roles import list_roles_tool
from .tools.setup import setup_tool

mcp: FastMCP = FastMCP(
    "rutherford",
    instructions=(
        "Rutherford orchestrates other agentic coding CLIs. Use `delegate` to hand a task to one "
        "CLI, `consensus` to ask several in parallel, `debate` to have several argue across rounds "
        "(returning the full transcript), and `capabilities`/`doctor` to see which are installed and "
        "authenticated. Delegations default to the configured default_safety_mode (read_only out of "
        "the box); write and yolo are explicit opt-in behind a trusted workspace. Long "
        "tasks can run as background jobs (mode=async), enumerated with list_jobs, polled with "
        "job_status / job_result, and cancelled with cancel_job."
    ),
)

_APP: AppContext | None = None


def get_app() -> AppContext:
    """Return the shared application context, building it lazily on first use.

    Logging is configured here on the lazy build (not in ``build_app_context``, which tests drive
    directly and want quiet), so any real entry point -- the ``rutherford`` console script via
    ``main`` or an embedded ``fastmcp run server.py:mcp`` that reaches a tool -- has structured
    logging set up (stderr, ``propagate=False``) before the first event, rather than dropping events
    or letting an unconfigured logger propagate to a host's root stdout handler.
    """
    global _APP
    if _APP is None:
        _APP = build_app_context()
        configure_logging(_APP.config.log_level, _APP.config.log_format)
    return _APP


async def _guarded(coro: Awaitable[str]) -> str:
    """Await a tool body, mapping Rutherford and unexpected errors to MCP tool errors.

    A :class:`RutherfordError` is structured and client-safe, so its payload passes through. An
    UNEXPECTED exception is the opposite: its text can carry filesystem paths, command fragments,
    or raw input, so the client gets a fixed message while the full traceback goes to the
    server-side log -- the operator keeps the diagnostic, the client does not get the internals.
    """
    try:
        return await coro
    except RutherfordError as exc:
        raise ToolError(error_payload_from(exc)) from exc
    except Exception as exc:  # never let an unexpected error crash the tool call
        logging.getLogger("rutherford.server").exception("unexpected error in a tool call")
        raise ToolError(tool_error(ErrorCode.INTERNAL, "internal server error; see the server log")) from exc


@mcp.tool
async def delegate(
    cli: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    mode: str = "sync",
    timeout_s: float | None = None,
    effort: str | None = None,
    session_id: str | None = None,
    include_raw: bool = False,
    trust_workspace: bool = False,
    persist: bool | None = None,
    external_tracking: bool = False,
    fallback: list[str] | None = None,
) -> str:
    """Delegate a task to one CLI and return its normalized result.

    `cli` is an adapter id (see `capabilities`); `model` is optional (the adapter's default
    otherwise). `safety_mode` is read_only | propose | write | yolo; when omitted, the configured
    `default_safety_mode` applies (read_only out of the box). write and yolo also need a trusted
    workspace (`trust_workspace=true` or a configured allowlist). With
    `mode="async"` a job id is returned; poll `job_status` / `job_result`. `session_id` resumes a
    prior session where the CLI supports it. `persist=true` keeps the run as a durable job under
    `.rutherford/jobs/<id>/` (state.toon + a Markdown answer); `None` follows `default_persistence`,
    and the kept run's `run_dir` is on the result. `effort` (low | medium | high | xhigh) is the
    producer "how hard may it think" hint, mapped to the CLI's native knob and reported as
    `effort_applied`; omit it to follow `default_effort`. `fallback` is an ordered list of alternate
    `cli` / `cli:model` targets tried if the primary fails on a retryable category (rate-limit, auth,
    timeout, a down CLI); the result's `target` is whoever answered and `fallback_chain` records the path.
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
            effort=effort,
            session_id=session_id,
            include_raw=include_raw,
            trust_workspace=trust_workspace,
            persist=persist,
            external_tracking=external_tracking,
            fallback=fallback,
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
    judge: Target | str | None = None,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    harvest_partial: bool = False,
    mode: str = "sync",
    include_raw: bool = False,
    persist: bool | None = None,
    external_tracking: bool = False,
) -> str:
    """Ask the same prompt of several targets in parallel and return every voice.

    `targets` is a list of `{cli, model}` objects (or `cli` / `cli:model` strings). Omit it, pass an
    empty list, or pass `"all"` to fan out to every installed + authenticated CLI at its default
    model (capped at `max_targets`; optional adapters like a local model are excluded unless named
    explicitly); the result's `skipped` list explains any adapter left out. Or name
    a saved `panel` (with optional `panel_overrides`) instead of `targets`; the two are mutually
    exclusive. Optional `stances` (parallel to `targets`) steer each voice: for | against | neutral,
    and cannot be combined with the auto-expanded panel. `synthesize=true` adds a server-side combined
    answer; when omitted it defaults to the configured `synthesize_default` (false out of the box).
    A `strategy` (`all-voices` | `unanimous` | `majority` | `plurality` |
    `weighted` | `parity-pair`), optionally with a `verdict_schema`, aggregates the voices into an
    `outcome` instead of returning them individually. Optional `judge` names a target (ideally a
    non-participant) that writes the synthesis or closing instead of the first voice; recorded as
    `synthesis_by` in the result. `effort` (low | medium | high | xhigh) is the producer effort hint
    applied to every voice; `time_budget_s` is a wall-clock harvest deadline for the WHOLE panel
    (distinct from each voice's `timeout_s`): at the deadline answered voices are kept and in-flight ones
    cut, aggregating over the harvest if `min_quorum` holds. `on_budget` is harvest (default) | continue
    (run all; budget advisory) | resume. With `mode="async"` a job id is returned.
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
            judge=judge,
            stances=stances,
            working_dir=working_dir,
            files=files,
            role=role,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
            effort=effort,
            time_budget_s=time_budget_s,
            on_budget=on_budget,
            harvest_partial=harvest_partial,
            mode=mode,
            include_raw=include_raw,
            persist=persist,
            external_tracking=external_tracking,
        )
    )


@mcp.tool
async def debate(
    prompt: str,
    targets: list[Target | str] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    rounds: int = 2,
    judge: Target | str | None = None,
    stances: list[str] | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    role: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool = True,
    timeout_s: float | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    mode: str = "sync",
    include_raw: bool = False,
    persist: bool | None = None,
    external_tracking: bool = False,
) -> str:
    """Have several targets argue a question across rounds and return the full transcript.

    `targets` is a list of `{cli, model}` objects (or `cli` / `cli:model` strings); a debate needs at
    least two. Or name a saved `panel` (with optional `panel_overrides`) instead of `targets`; the two
    are mutually exclusive. `rounds` (default 2) is how many passes the panel makes: round one is each
    voice's independent answer, and each later round shows a voice the others' latest positions and
    asks it to rebut and revise. Optional `stances` (parallel to `targets`) keep a voice arguing for |
    against | neutral throughout. `synthesize=true` (default) adds a closing summary. Optional `judge`
    names a target (ideally a non-participant) that writes the closing synthesis instead of the first
    voice; recorded as `synthesis_by` in the result. The result's `rounds` hold every voice's answer
    at every round, so the discussion is fully retraceable. `effort` (low | medium | high | xhigh) is the
    producer effort hint applied to every turn; `time_budget_s` is a wall-clock budget for the WHOLE
    debate, enforced at ROUND boundaries (the transcript-so-far is finalized once it is reached; round 1
    always completes). `on_budget` is harvest (default) | continue (run every round) | resume. With
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
            judge=judge,
            stances=stances,
            working_dir=working_dir,
            files=files,
            role=role,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
            effort=effort,
            time_budget_s=time_budget_s,
            on_budget=on_budget,
            mode=mode,
            include_raw=include_raw,
            persist=persist,
            external_tracking=external_tracking,
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
async def list_jobs() -> str:
    """List background jobs (id, kind, status, timestamps), newest first."""
    return await _guarded(list_jobs_tool(get_app()))


@mcp.tool
async def cancel_job(job_id: str) -> str:
    """Cancel a running or pending background job, killing its CLI process tree."""
    return await _guarded(cancel_job_tool(get_app(), job_id=job_id))


@mcp.tool
async def review(
    targets: list[Target | str] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    paths: list[str] | None = None,
    diff: str | None = None,
    role: str = "codereviewer",
    working_dir: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
) -> str:
    """Review a diff or a set of files across one or more targets (read-only). Provide diff or paths.

    `targets` is a list of `{cli, model}` objects (or `cli` / `cli:model` strings). Name a list of
    `targets` or a saved `panel` (with optional `panel_overrides`); they are mutually exclusive.
    `synthesize` defaults to the configured `synthesize_default` (false out of the box).
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
    timeout_s: float | None = None,
) -> str:
    """Ask one target to produce an ordered implementation plan for a goal (always read-only)."""
    return await _guarded(
        plan_tool(
            get_app(),
            cli=cli,
            goal=goal,
            model=model,
            role=role,
            working_dir=working_dir,
            files=files,
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
    default_persistence: str | None = None,
    scope: str = "global",
) -> str:
    """Scaffold a starter config and panel from the CLIs you have installed and signed in.

    By default this is a dry run: it returns the proposed files (with their full contents) so you can
    review them. Pass `apply=true` to write them; an existing file is kept unless `force=true`.
    `default_persistence` (`ephemeral` | `job`) answers the first-run question of whether runs are kept
    as durable jobs by default (F2). `scope` is `global` (per-user, default) or `project` (this
    workspace's `.rutherford/`) -- use `project` to answer the first-run hint for the current workspace.
    """
    return await _guarded(
        setup_tool(
            get_app(),
            apply=apply,
            force=force,
            safety_mode=safety_mode,
            trusted_workspaces=trusted_workspaces,
            panel_name=panel_name,
            default_persistence=default_persistence,
            scope=scope,
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
    # Structured logs go to stderr (stdout is the MCP channel); configured from the loaded config.
    configure_logging(_APP.config.log_level, _APP.config.log_format)
    # Suppress the FastMCP startup banner so a stdio client's stderr log stays clean.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
