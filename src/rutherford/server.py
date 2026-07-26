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

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from .config.loader import default_global_config_path, load_config
from .config.schema import RutherfordConfig
from .config.trust import (
    TrustResult,
    read_global_trusted_workspaces,
    trust_workspace,
    untrust_workspace,
)
from .context import AppContext, build_app_context, error_payload_from, tool_error
from .domain.enums import ActivityEventKind
from .domain.error_codes import ErrorCode
from .domain.errors import ConfigError, RutherfordError
from .domain.models import ActivityEvent
from .runtime.logging import configure_logging
from .services.delegation import ActivityCallback
from .tools.analyze import analyze_tool
from .tools.capabilities import capabilities_tool, doctor_tool
from .tools.consensus import consensus_tool
from .tools.continue_job import continue_job_tool
from .tools.debate import debate_tool
from .tools.delegate import delegate_tool
from .tools.discover import discover_tool
from .tools.jobs import activity_tool, cancel_job_tool, job_result_tool, job_status_tool, list_jobs_tool
from .tools.panels import reload_panels_tool
from .tools.plan import plan_tool
from .tools.review import review_tool
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
        "time here? `setup` shows where config lives and scaffolds a starter config.toml, and `discover` "
        "finds ACP agents you already have installed (via the community registry) and proposes config for them."
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


async def _report_progress(context: Context, progress: float, total: float | None, message: str | None) -> None:
    """Send one MCP progress notification, swallowing any transport error (N1, item 3 push, best-effort).

    A no-op on the client side unless the caller supplied a ``progressToken`` (FastMCP gates it), so this is
    always safe to call; ``message`` is ``None`` when an event carried no text.
    """
    with contextlib.suppress(Exception):
        await context.report_progress(progress, total, message)


def make_progress_pusher(context: Context) -> ActivityCallback:
    """Build the live-activity sink that pushes a sync call's :class:`ActivityEvent`s as MCP progress (N1).

    The PUSH half of N1 (the ``activity`` tool is the poll half): each event becomes a ``report_progress``
    notification so the caller sees a sync panel advance live. Only meaningful for a synchronous call -- an
    async job returns a job id immediately, so its progress is polled, not pushed. ``progress`` counts the
    voices finished and ``total`` the panel's declared width once known, so a client can show a real
    fraction. The notification is fire-and-forget (scheduled, not awaited) so a slow client never stalls the
    run; the tasks are tracked in a set so they are not garbage-collected before they send. FastMCP gates the
    whole channel on a client-supplied ``progressToken``, so this is silent when the caller did not opt in.
    """
    total: int | None = None
    done = 0
    resolved: set[str] = set()  # correlation ids already counted, so a voice is counted at most once
    pending: set[asyncio.Task[None]] = set()

    def push(event: ActivityEvent) -> None:
        nonlocal total, done
        if event.kind is ActivityEventKind.PANEL_STARTED and event.declared:
            # Only a consensus resolves exactly one voice per declared seat (each ends finished OR cut), so
            # its done/declared is a true fraction. A debate resolves one per TURN across rounds (turns
            # exceed the width, and the count varies with an early stop), so it stays indeterminate -- a total
            # here would be passed and then overshot. The push fraction is consensus-only by design.
            if event.tool == "consensus":
                total = event.declared
        elif event.kind in (ActivityEventKind.VOICE_FINISHED, ActivityEventKind.CUT):
            # A voice is resolved either way -- it finished, or a budget deadline cut it. Count each voice
            # ONCE, keyed by its correlation id, so a harvested voice does not double-count past the total.
            cid = event.correlation_id
            if cid is None or cid not in resolved:
                if cid is not None:
                    resolved.add(cid)
                done += 1
        elif event.kind is ActivityEventKind.PANEL_FINISHED and total is not None:
            done = int(total)  # the panel is complete: snap to 100% regardless of any cut/escape accounting
        cap = float(total) if total else None
        task = asyncio.create_task(_report_progress(context, float(done), cap, event.message or None))
        pending.add(task)
        task.add_done_callback(pending.discard)

    return push


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
    effort: str | None = None,
    fallback: list[Any] | None = None,
    allow_model_fallback: bool = True,
    persist: bool | None = None,
    session_id: str | None = None,
    external_tracking: bool = False,
    mode: str = "sync",
) -> str:
    """Delegate a task to one ACP agent and return its normalized result.

    `cli` is an agent id (see `capabilities`); `model` is optional (the agent's default otherwise).
    `safety_mode` is read_only | propose | write | yolo; when omitted, the configured `default_safety_mode`
    applies (read_only out of the box). write and yolo also need a trusted workspace (`trust_workspace=true`
    or a configured allowlist). `files` lists paths to put in scope. `role` names a persona (see
    `list_roles`) whose system prompt is prepended to `prompt`. `effort` (low | medium | high | xhigh) asks
    the agent to spend more reasoning where it has a knob (codex/cursor via the model id, cline via
    --thinking, junie via env); a reported no-op for an agent with none. Omitted, the configured
    `default_effort` (per-agent or global) applies. `fallback` is an ordered list of alternate targets
    (`cli` / `cli:model` strings or `{cli, model}` objects) tried when the primary fails on a
    re-execution-safe failure (a spawn/handshake failure that never ran the prompt); a benched alternate is
    skipped and `fallback_chain` records the path. A write/yolo delegation never falls back.
    `allow_model_fallback` (default true) first retries the same agent on its configured fallback model on a
    model-unavailable failure, where it has one. `persist` keeps this run as a durable job under
    `<jobs_dir>/<run_id>/` (`state.json` + answer / diff artifacts); `None` follows `default_persistence`
    (`ephemeral` out of the box), `true` / `false` force it. `session_id` resumes a prior agent session: pass
    the `session_id` from an earlier delegate result and the agent reloads that conversation (ACP
    `session/load`) instead of starting fresh, so a follow-up turn continues it; agents that do not persist
    their own sessions fail `RESUME_FAILED`. `mode="async"` runs the turn as a background job and returns a
    `job_id` (poll with `job_status` / `job_result`); `mode="sync"` awaits it.
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
            effort=effort,
            fallback=fallback,
            allow_model_fallback=allow_model_fallback,
            persist=persist,
            session_id=session_id,
            external_tracking=external_tracking,
            mode=mode,
        )
    )


@mcp.tool
async def continue_job(
    job_id: str,
    prompt: str,
    model: str | None = None,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    timeout_s: float | None = None,
    trust_workspace: bool = False,
    role: str | None = None,
    effort: str | None = None,
    rounds: int = 2,
    persist: bool = True,
    mode: str = "sync",
) -> str:
    """Continue a completed durable job with a new direction, picking up where the kept run left off.

    `job_id` is the id of a kept run under `<jobs_dir>/` (the `run_dir` name a persisted result carries). A
    `delegate` job resumes its one session (else re-injects the prior prompt + answer); a `consensus` panel
    resumes each voice's session and re-aggregates under the recorded strategy; a `debate` resumes each seat's
    session and argues `rounds` MORE rounds (`rounds` is ignored for the other kinds). The parent's record
    supplies the roster, model, working dir, role, files, and -- for a panel -- the strategy / stances /
    per-seat steering, all inherited unless overridden here. A seat whose agent cannot reload its ACP session
    is recorded as a failed voice, never silently dropped. The continuation is a fresh run linked to the
    parent (`continued_from`) -- the parent is never mutated. The trust gate is re-applied fresh and defaults
    to `read_only` (panels are read-only deliberation regardless). `persist` (default true) keeps the
    continuation as its own durable child job. `mode="async"` runs it as a background job and returns a
    `job_id`.
    """
    return await _guarded(
        continue_job_tool(
            get_app(),
            job_id=job_id,
            prompt=prompt,
            model=model,
            working_dir=working_dir,
            files=files,
            safety_mode=safety_mode,
            timeout_s=timeout_s,
            trust_workspace=trust_workspace,
            role=role,
            effort=effort,
            rounds=rounds,
            persist=persist,
            mode=mode,
        )
    )


@mcp.tool
async def consensus(
    prompt: str,
    targets: list[Any] | str | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    strategy: str | None = None,
    verdict_schema: dict[str, Any] | None = None,
    judge: Any | None = None,
    require_independent_judge: bool = False,
    require_dissent: bool = False,
    discount_correlated: bool = False,
    stances: list[str] | None = None,
    expand_all: bool = False,
    working_dir: str | None = None,
    files: list[str] | None = None,
    safety_mode: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
    role: str | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    persist: bool | None = None,
    external_tracking: bool = False,
    mode: str = "sync",
    ctx: Context | None = None,
) -> str:
    """Ask the same prompt of several ACP agents in parallel and reduce the voices.

    `targets` is a list of `{cli, model}` objects or `cli` / `cli:model` strings; each runs as its own ACP
    session concurrently. Omit `targets`, pass an empty list, pass the sentinel `"all"`, or set
    `expand_all=true` to fan out to every registered agent (each at its default model, capped at
    `max_targets`); the result's `skipped` field explains any agent left out. Or name a saved `panel` (with
    optional `panel_overrides`) to reuse a stored roster + strategy instead of `targets`; they are mutually
    exclusive (see `reload_panels`). A `{cli, model}` target may
    also carry per-seat `role` / `label` / `weight` / `parity` / `stance`. With a `strategy` other than
    `all-voices` (`unanimous` | `majority` | `plurality` | `weighted` | `parity-pair` | `rank`, optionally
    with a `verdict_schema`), each voice is asked for a verdict and the panel collapses to one outcome
    (`StrategyResult`) instead of every voice. `rank` is a two-round protocol (F4b): every voice answers, then
    ranks the OTHER answers anonymized and self-excluded, aggregated by Borda mean-rank into a `rank`
    leaderboard with a pairwise agreement matrix and concordance; `require_dissent` surfaces each non-winning
    position on its `dissent`. `discount_correlated=true` (F3 vote-math, opt-in) down-weights correlated votes
    by model-family lineage (vendor fallback) so a panel of "one model in N CLI costumes" counts as one
    effective vote under `majority` / `plurality` / `weighted` (each voice's `lineage_weight` shows it). Optional
    `stances` (parallel to `targets`) steer each voice and cannot combine with the auto-expanded panel.
    `synthesize` (defaults to `synthesize_default`, off
    out of the box) adds a server-side combined answer (`all-voices` only); `judge` names the seat that
    writes it. `timeout_s` applies to every voice; one failing voice is a failed result, never an aborted
    panel. Consensus is read-only deliberation: a `safety_mode` beyond `read_only` (`propose` / `write` /
    `yolo`) is refused -- there is no coherent merge of edits from several agents into one tree -- so route
    write / propose work through `delegate` (a single agent isolated in a worktree sandbox). `role` names a
    persona (see `list_roles`) prepended to the prompt every voice
    receives. `effort` (low | medium | high | xhigh) asks every voice to spend more reasoning where it has a
    knob. `time_budget_s` is a wall-clock deadline for the WHOLE panel (distinct from each voice's
    `timeout_s`): at the deadline answered voices are kept, in-flight ones cut, and the panel aggregates over
    the harvest if `min_quorum` usable remain (`stop_reason="budget"`, with a `rollup`); below `min_quorum`
    is `BUDGET_EXHAUSTED`. `on_budget` is harvest | continue | resume (default `default_on_budget`). `persist`
    keeps the panel as a durable job (F2): a parent `state.json` linking a child record per voice, plus
    `voices/voice-N.md` artifacts; `None` follows `default_persistence`, `true` / `false` force it.
    `mode="async"` runs the panel as a background job and returns a `job_id` (poll with `job_status` /
    `job_result`); `mode="sync"` awaits it.
    """
    return await _guarded(
        consensus_tool(
            get_app(),
            prompt=prompt,
            targets=targets,
            panel=panel,
            panel_overrides=panel_overrides,
            strategy=strategy,
            verdict_schema=verdict_schema,
            judge=judge,
            require_independent_judge=require_independent_judge,
            require_dissent=require_dissent,
            discount_correlated=discount_correlated,
            stances=stances,
            expand_all=expand_all,
            working_dir=working_dir,
            files=files,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
            role=role,
            effort=effort,
            time_budget_s=time_budget_s,
            on_budget=on_budget,
            persist=persist,
            external_tracking=external_tracking,
            mode=mode,
            # N1 (item 3): on a sync call, push live progress as voices finish (gated on the client's
            # progressToken; silent otherwise). An async job polls ``activity`` instead, so no pusher.
            on_activity=make_progress_pusher(ctx) if ctx is not None else None,
        )
    )


@mcp.tool
async def debate(
    prompt: str,
    targets: list[Any] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    rounds: int = 2,
    judge: Any | None = None,
    require_independent_judge: bool = False,
    carry_forward: bool = False,
    track_convergence: bool = False,
    working_dir: str | None = None,
    safety_mode: str | None = None,
    synthesize: bool = True,
    timeout_s: float | None = None,
    role: str | None = None,
    effort: str | None = None,
    time_budget_s: float | None = None,
    on_budget: str | None = None,
    persist: bool | None = None,
    external_tracking: bool = False,
    mode: str = "sync",
    ctx: Context | None = None,
) -> str:
    """Have several ACP agents argue a question across rounds and return the full transcript.

    `targets` is a list of `{cli, model}` objects or `cli` / `cli:model` strings; a debate needs at least
    two. Or name a saved `panel` (with optional `panel_overrides`) for a stored roster instead of `targets`;
    they are mutually exclusive (`rounds` / `judge` stay call args). Each voice keeps ONE persistent ACP
    session across all `rounds`: round one is each voice's
    independent answer, and each later round shows a voice the others' latest positions and asks it to
    revise -- the agent remembers its own prior reasoning in-session, so only the delta is sent.
    `carry_forward=true` instead re-sends the FULL prior transcript verbatim each round (for a weaker session
    memory; bounded by `time_budget_s`). `track_convergence=true` asks each voice for a one-word verdict each
    round and stops early when the panel CONVERGES (a unanimous verdict) or STALLS (the decision holds for the
    configured tolerance); the `outcome` field reports the termination reason (converged / stalled /
    unresolved / budget / quorum_lost) and the final decision.
    `synthesize=true` (default) adds a closing summary; `judge` names a target to write it. A debate is
    read-only deliberation: a `safety_mode` beyond `read_only` (`propose` / `write` / `yolo`) is refused --
    the voices run on persistent sessions in the working directory with no per-turn sandbox -- so route write /
    propose work through `delegate` (a single agent isolated in a worktree sandbox). `role` names a
    persona (see `list_roles`) prepended to the opening prompt every voice argues from. `effort` (low |
    medium | high | xhigh) asks every voice to spend more reasoning where it has a knob. `time_budget_s` is a
    wall-clock deadline for the WHOLE debate enforced at round boundaries: a round still in flight at the
    deadline is cut and the transcript so far is finalized (`stop_reason="budget"`, with a `rollup`);
    `on_budget` is harvest | continue | resume (default `default_on_budget`; `continue` runs every round to
    completion). `persist` keeps the debate as a durable job (F2): a parent `state.json` plus the full
    `transcript.md`; `None` follows `default_persistence`, `true` / `false` force it. `mode="async"` runs the
    debate as a background job and returns a `job_id` (poll with `job_status` / `job_result`); `mode="sync"`
    awaits it.
    """
    return await _guarded(
        debate_tool(
            get_app(),
            prompt=prompt,
            targets=targets,
            panel=panel,
            panel_overrides=panel_overrides,
            rounds=rounds,
            judge=judge,
            require_independent_judge=require_independent_judge,
            carry_forward=carry_forward,
            track_convergence=track_convergence,
            working_dir=working_dir,
            safety_mode=safety_mode,
            synthesize=synthesize,
            timeout_s=timeout_s,
            role=role,
            effort=effort,
            time_budget_s=time_budget_s,
            on_budget=on_budget,
            persist=persist,
            external_tracking=external_tracking,
            mode=mode,
            on_activity=make_progress_pusher(ctx) if ctx is not None else None,
        )
    )


@mcp.tool
async def capabilities() -> str:
    """List the ACP agents Rutherford can drive (static roster; no spawn).

    Each agent includes id, display name, launch command, provider, configured `default_model` /
    `fallback_model`, `model_selection` (`launch_argv` for Cursor-style launch flags, else
    `in_session`), and `effort_capable`. Model resolution is: explicit `model` -> agent
    `default_model` -> agent-native default. For live advertised model ids, use
    `doctor(agent=<id>, connect_only=true)` -- capabilities never probes an agent.
    """
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
async def review(
    targets: list[Any] | None = None,
    panel: str | None = None,
    panel_overrides: dict[str, Any] | None = None,
    paths: list[str] | None = None,
    diff: str | None = None,
    role: str = "principal-reviewer",
    working_dir: str | None = None,
    synthesize: bool | None = None,
    timeout_s: float | None = None,
) -> str:
    """Review a diff or a set of files across one or more ACP agents (read-only). Provide `diff` or `paths`.

    A read-only `consensus` under the `principal-reviewer` persona: each agent reviews the code and the
    panel returns every voice plus a combined verdict. `targets` is a list of `{cli, model}` objects (or
    `cli` / `cli:model` strings); or name a saved `panel` (with optional `panel_overrides`) instead -- the
    two are mutually exclusive. Provide `diff` (a unified diff, inlined into the prompt) or `paths` (files put
    in scope for the agents to read). `synthesize` defaults on (the combined verdict); pass `false` for the
    raw per-voice reviews. Always read-only -- a review never mutates the tree.
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
    role: str = "architect",
    working_dir: str | None = None,
    files: list[str] | None = None,
    timeout_s: float | None = None,
) -> str:
    """Ask one ACP agent for an implementation plan for `goal` under the `architect` persona (read-only).

    A read-only `delegate` with the `architect` (planner) persona prepended: the agent designs an approach
    rather than implementing it. `cli` is an agent id (see `capabilities`); `model` is optional. `files`
    lists paths to put in scope. Always read-only -- planning never mutates the tree; implementing the plan
    is `delegate` in write mode.
    """
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
async def reload_panels() -> str:
    """Re-read saved panels from disk (after editing a `panels.toon`) and list those now available.

    Returns `{reloaded, count, panels: [{name, description, target_count}]}`. Panels are discovered under
    `~/.rutherford/panels.toon`, the project `.rutherford/panels.toon`, and `$RUTHERFORD_CONFIG_DIR`, merged
    by name (closest scope wins). A malformed panels file raises `PANEL_INVALID` naming the file and seat.
    """
    return await _guarded(reload_panels_tool(get_app()))


@mcp.tool
async def setup(
    scope: str = "project", write: bool = False, trust_workspace: bool = False, install_adapters: bool = False
) -> str:
    """Show where config lives and scaffold a starter `config.toml`; the first-run helper.

    `scope` is `project` (`<cwd>/.rutherford/config.toml`) or `global` (the platform config dir's
    `config.toml`). It returns the proposed starter `content` (the most useful settings at their effective
    defaults) and the resolved `path`, plus a snapshot of the agents you already have. Pass `write=true` to
    create the file -- it never overwrites an existing one (`already_exists=true`, `written=false`).
    `trust_workspace=true` adds the current directory to `trusted_workspaces` so write/yolo delegations are
    permitted there.

    The `adapters` block reports agents whose underlying CLI is installed but whose npm ACP adapter shim is
    not (codex needs `codex-acp`, claude_code needs `claude-agent-acp`, pi needs `pi-acp` -- what `doctor`
    flags as `not_installed` with an install hint). Pass `install_adapters=true` to run `npm i -g <package>`
    for each of those automatically (an explicit, opt-in machine change; off by default).
    """
    return await _guarded(
        setup_tool(
            get_app(), scope=scope, write=write, trust_workspace=trust_workspace, install_adapters=install_adapters
        )
    )


@mcp.tool
async def discover(refresh: bool = False, probe: bool = True, write: bool = False, scope: str = "project") -> str:
    """Find installed ACP agents via the community registry and propose `[agents.<id>]` config for them.

    The registry-driven companion to `setup`/`doctor`. It fetches the ACP agent registry (cached under
    `~/.rutherford/acp-registry.json` for offline use), detects which registry agents are ALREADY installed
    here -- scanning PATH plus curated install dirs (`~/.local/bin`, `~/.cargo/bin`, `~/.<vendor>/bin`),
    never downloading or running `npx` -- and (with `probe=true`, the default) drives each found agent with a
    real read-only ACP round trip so the proposal only includes ones that actually answer. Returns the
    discovered agents and a proposed `[agents.<id>]` config block for the new drivers. `write=true` appends
    that block to the config for `scope` (`project` -> `<cwd>/.rutherford/config.toml`, `global` -> the
    platform path), creating the file if needed and never overwriting an existing section. `refresh`
    re-fetches the registry. Use this to adopt an ACP agent (or bridge) Rutherford does not ship as a built-in.
    """
    return await _guarded(discover_tool(get_app(), refresh=refresh, probe=probe, write=write, scope=scope))


@mcp.tool
async def doctor(agent: str | None = None, timeout_s: float = 60.0, connect_only: bool = False) -> str:
    """Probe each agent (or one named `agent`) with a real read-only ACP round trip and report conformance.

    The trustworthy health check for ACP agents: whether each spawns, handshakes, and answers. Each report
    is ok / no_answer / model_unavailable / handshake_failed / not_installed / error. `model_unavailable`
    means spawn + handshake succeeded (the agent is reachable) but the harness/provider rejected the model on
    the turn (a model/provider config issue, e.g. a Claude Code on AWS Bedrock / Vertex), so it is NOT reported
    as a broken agent. Slower
    than `capabilities` (it makes a real call per agent); run it to see which of the roster actually drive on
    this machine. `connect_only`
    runs the lighter handshake-only check (spawn + handshake, no prompt) and reports reachable /
    handshake_failed / not_installed plus each agent's advertised models -- it shows whether Rutherford can
    talk to and configure an agent even when a model call would fail for a reason outside ACP (an auth /
    entitlement / quota issue, e.g. Grok without a SuperGrok subscription).

    When an agent (codex / claude_code / pi) launches a separate npm ACP adapter shim and that shim is not
    installed but its underlying CLI is (you have `codex`/`claude`/`pi`), the report adds an `install_hint`
    with the exact `npm i -g <package>` command instead of a flat not_installed -- run that, or
    `setup install_adapters=true`, to set the adapter up.
    """
    return await _guarded(doctor_tool(get_app(), agent=agent, timeout_s=timeout_s, connect_only=connect_only))


@mcp.tool
async def list_jobs() -> str:
    """List the background jobs Rutherford is tracking (id, tool, status, summary, timestamps), newest first.

    The light listing -- no heavy result. Fetch a finished job's result with `job_result`. Jobs are
    in-memory: a finished one is evicted after `job_ttl_s`, and a restart clears them all.
    """
    return await _guarded(list_jobs_tool(get_app()))


@mcp.tool
async def analyze(report: str = "historical_agreement") -> str:
    """Analyze the kept run corpus (read-only). `report="historical_agreement"` is the default and only report.

    `historical_agreement` scans the consensus panels you kept (persist=true / default_persistence=job) and
    reports how often two DISTINCT model lineages reached the same verdict when they co-voted -- an
    OBSERVATIONAL signal for your roster choice (e.g. a lineage that never adds a dissent), NOT a vote discount:
    agreement is not correctness, so down-weighting agreeing lineages would punish them for being right
    together. An empty corpus returns an empty report whose notes explain how to build one.
    """
    return await _guarded(analyze_tool(get_app(), report=report))


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


def _init(args: list[str]) -> None:
    """First-run setup CLI (``python -m rutherford init [--global] [--yes]``).

    Scaffolds a starter ``config.toml`` at the effective defaults -- ``<cwd>/.rutherford/config.toml`` by
    default, or the platform global config path with ``--global`` -- and shows the registered agents. It NEVER
    clobbers an existing file (edit it, or remove it and re-run); ``--yes`` skips the confirmation prompt. The
    config scaffolding is the same the ``setup`` MCP tool writes. Afterwards, run the ``doctor`` tool from an
    MCP client to see which agents are installed and actually answer over ACP (the trustworthy health signal,
    which a static scaffold cannot give). Auto-detect is disabled here so the command is fast and deterministic.
    """
    scope = "global" if "--global" in args else "project"
    assume_yes = "--yes" in args or "-y" in args
    # ``init`` is the bootstrap command, so it must not refuse to scaffold just because an EXISTING config is
    # malformed -- in particular a broken project ``config.toml`` must never block ``init --global`` (a
    # different scope). Try the effective config for the roster snapshot + defaults; on a load error fall back
    # to the built-in defaults and warn, rather than exiting. Auto-detect is off so the command stays fast.
    try:
        config = load_config().model_copy(update={"auto_detect_local_models": False})
    except ConfigError as exc:
        print(f"rutherford: ignoring an invalid existing config ({exc}); scaffolding from defaults", file=sys.stderr)
        config = RutherfordConfig(auto_detect_local_models=False)
    app = build_app_context(config=config)
    path = default_global_config_path() if scope == "global" else Path.cwd() / ".rutherford" / "config.toml"
    agents = app.descriptors.ids()
    print(f"rutherford: {len(agents)} built-in agent(s): {', '.join(agents)}")
    print(f"config target ({scope}): {path}")
    if path.exists():
        print("a config already exists there; it will not be overwritten -- edit it, or remove it and re-run.")
        return
    if not assume_yes and input("\nwrite a starter config.toml there? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted; nothing written.")
        return
    asyncio.run(setup_tool(app, scope=scope, write=True))
    print(f"wrote {path}")
    print("next: run the `doctor` tool from your MCP client to see which agents answer over ACP.")


def _discover(args: list[str]) -> None:
    """Registry-driven discovery CLI (``python -m rutherford discover [--global] [--write] [--no-probe] [--refresh]``).

    Fetches the ACP registry, detects installed agents (PATH + curated install dirs; never downloads),
    probes the ones found (unless ``--no-probe``), and prints the discovered agents plus a proposed
    ``[agents.<id>]`` config block for the new drivers. ``--write`` appends that block to the project config
    (``--global`` for the global one), never clobbering an existing section. The same logic the ``discover``
    MCP tool runs, surfaced for the terminal so a user can adopt agents before wiring up an MCP client.
    """
    scope = "global" if "--global" in args else "project"
    write = "--write" in args
    probe = "--no-probe" not in args
    refresh = "--refresh" in args
    try:
        config = load_config().model_copy(update={"auto_detect_local_models": False})
    except ConfigError as exc:
        print(f"rutherford: ignoring an invalid existing config ({exc}); discovering with defaults", file=sys.stderr)
        config = RutherfordConfig(auto_detect_local_models=False)
    app = build_app_context(config=config)
    print("rutherford: fetching the ACP registry and probing installed agents (this can take a moment)...")
    out = asyncio.run(discover_tool(app, refresh=refresh, probe=probe, write=write, scope=scope))
    print(out)


def _trust_cli(args: list[str]) -> None:
    """Trust CLI (``python -m rutherford trust [--list] [PATH]``): add cwd/PATH to the global allowlist.

    Edits the platform global ``config.toml`` only. ``--list`` prints the current global
    ``trusted_workspaces`` and exits without writing. An explicit command is consent; there is no
    confirmation prompt.
    """
    flags = {a for a in args if a.startswith("-")}
    unknown = flags - {"--list"}
    if unknown:
        print(f"rutherford trust: unknown option(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        print("rutherford trust: usage: trust [--list] [PATH]", file=sys.stderr)
        raise SystemExit(2)
    if "--list" in flags:
        if any(not a.startswith("-") for a in args):
            print("rutherford trust: --list does not take a PATH", file=sys.stderr)
            raise SystemExit(2)
        _list_trusted_cli()
        return
    path_args = [a for a in args if not a.startswith("-")]
    if len(path_args) > 1:
        print("rutherford trust: usage: trust [--list] [PATH]", file=sys.stderr)
        raise SystemExit(2)
    workspace = path_args[0] if path_args else None
    try:
        result = trust_workspace(workspace)
    except ConfigError as exc:
        print(f"rutherford trust: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    _print_trust_result("trust", result)


def _untrust_cli(args: list[str]) -> None:
    """Untrust CLI (``python -m rutherford untrust [PATH]``): remove cwd/PATH from the global allowlist."""
    path_args = [a for a in args if not a.startswith("-")]
    if len(path_args) > 1 or any(a.startswith("-") for a in args):
        print("rutherford untrust: usage: untrust [PATH]", file=sys.stderr)
        raise SystemExit(2)
    workspace = path_args[0] if path_args else None
    try:
        result = untrust_workspace(workspace)
    except ConfigError as exc:
        print(f"rutherford untrust: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    _print_trust_result("untrust", result)


def _list_trusted_cli() -> None:
    """Print the global ``trusted_workspaces`` allowlist (``rutherford trust --list``)."""
    try:
        path, workspaces = read_global_trusted_workspaces()
    except ConfigError as exc:
        print(f"rutherford trust: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"rutherford: global config: {path}")
    if not workspaces:
        print("trusted_workspaces: (empty)")
        return
    print(f"trusted_workspaces ({len(workspaces)}):")
    for entry in workspaces:
        print(f"  - {entry}")


def _print_trust_result(command: str, result: TrustResult) -> None:
    """Print a one-shot trust/untrust outcome to stdout."""
    print(f"rutherford {command}: {result.action} -- {result.workspace}")
    print(f"global config: {result.config_path}")
    if result.note:
        print(result.note)
    if result.trusted_workspaces:
        print(f"trusted_workspaces ({len(result.trusted_workspaces)}):")
        for entry in result.trusted_workspaces:
            print(f"  - {entry}")
    else:
        print("trusted_workspaces: (empty)")


def main() -> None:
    """Console entry point: start the stdio MCP server. ``--smoke`` builds the app and exits, no server loop;
    ``init`` scaffolds a starter config and exits; ``discover`` finds installed ACP agents and exits;
    ``trust`` / ``untrust`` edit the global ``trusted_workspaces`` allowlist and exit.

    The smoke path is the entrypoint health check (``just smoke``): it loads config and builds the full app
    context -- exercising config validation and registry build -- then prints a line and returns instead of
    blocking on ``mcp.run``. It disables live local-backend probing so the check is fast and deterministic.
    """
    argv = sys.argv[1:]
    if argv and argv[0] == "init":
        _init(argv[1:])
        return
    if argv and argv[0] == "discover":
        _discover(argv[1:])
        return
    if argv and argv[0] == "trust":
        _trust_cli(argv[1:])
        return
    if argv and argv[0] == "untrust":
        _untrust_cli(argv[1:])
        return
    smoke = "--smoke" in sys.argv
    try:
        global _APP
        config = load_config().model_copy(update={"auto_detect_local_models": False}) if smoke else None
        _APP = build_app_context(config=config)
    except ConfigError as exc:
        print(f"rutherford: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    configure_logging(_APP.config.log_level, _APP.config.log_format)
    if smoke:
        print(f"rutherford: smoke ok -- {len(_APP.descriptors)} agents registered")
        return
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
