# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``login`` tool: sign an ACP agent in once, through the protocol's own ``authenticate`` call.

Some agents (Google Antigravity is the motivating one) open no session until a client has called ACP
``authenticate``; after that they cache the credential themselves and every later session just works. No
ordinary Rutherford call signs in, because a delegation that popped a browser on the user's machine as a side
effect would be a surprise. This tool is the explicit, opt-in place where it happens.

The flow has to survive the fact that a sign-in waits on a person. ``authenticate`` for a browser OAuth method
blocks until the user approves, which can take minutes, and the link they need is printed by the agent while
that call is still pending. So the sign-in runs as a background job, and the tool waits only briefly for the
agent to print its link before returning it with the job id. The caller hands the link to the user and polls
``job_status`` / ``job_result`` for the outcome.

Called without ``method`` it is read-only: it reports whether the agent is already signed in and, if not, the
methods it offers. Only ``agent``-kind methods are performed; ``env_var`` and ``terminal`` methods are
completed by the user and are answered with what to set or run.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..acp.auth import AuthMethodInfo
from ..acp.descriptors import AgentDescriptor
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession
from ..context import AppContext, tool_success
from ..domain.enums import JobStatus, SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..services.jobs import JobRecord, JobStore
from .common import ensure_known_agent
from .jobs import make_summary

#: How often the tool checks for the agent's sign-in link (or an early finish) while it waits.
_POLL_INTERVAL_S = 0.25
#: Bounds on the caller's ``wait_s`` and ``timeout_s``, so a typo cannot hold a tool call open for an hour.
_MAX_WAIT_S = 120.0
_MAX_TIMEOUT_S = 1800.0

#: Labels the links as the agent's own words. Rutherford neither generated nor opened them.
_URL_NOTE = (
    "signin_urls were printed by the agent on its stderr during the sign-in; Rutherford did not open them. "
    "Open the first one to sign in (the agent may also have opened a browser itself), then poll job_status / "
    "job_result with this job_id."
)


async def login_tool(
    app: AppContext,
    *,
    agent: str,
    method: str | None = None,
    wait_s: float = 20.0,
    timeout_s: float = 300.0,
) -> str:
    """Report an agent's sign-in state and methods, or sign it in with ``method`` as a background job.

    Without ``method``: opens a session (no prompt). A session opens -> ``status=authenticated``, meaning no ACP
    sign-in is needed (a turn can still need a provider key, which ``doctor`` checks). Not signed
    in -> ``status=method_required`` with the advertised ``methods``. With ``method`` on an ``agent``-kind
    method: starts ``authenticate`` in a background job, waits up to ``wait_s`` for the agent to print a
    sign-in link or finish, and returns ``status=pending`` (``job_id`` + ``signin_urls``) or the finished
    outcome. ``timeout_s`` bounds the ``authenticate`` step (the wait on the person signing in); the launch and
    the confirming ``session/new`` keep the agent's own handshake budget. While a sign-in is running for an
    agent, a further ``login`` for it joins that job rather than starting a second one (``pending``, or the
    job's outcome if it ends within ``wait_s``).
    ``env_var`` / ``terminal`` methods return what the user has to set or run instead
    (``status=configure_env`` / ``run_in_terminal``).
    """
    ensure_known_agent(app.descriptors, agent)
    if not 0 <= wait_s <= _MAX_WAIT_S:
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"wait_s must be between 0 and {_MAX_WAIT_S:g}")
    if not 0 < timeout_s <= _MAX_TIMEOUT_S:
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"timeout_s must be above 0 and at most {_MAX_TIMEOUT_S:g}")
    # A sign-in already running for this agent is joined, not duplicated: a caller that retries after a
    # `pending` answer must not open a second consent screen racing the first to write the credential cache.
    # The lock spans the check, the state check, and the submit, so concurrent calls serialize on it; the wait
    # for the link happens after it is released, so a joining caller is answered without waiting on that.
    async with _agent_lock(app, agent):
        active = await _active_signin(app, agent)
        if active is None:
            outcome: str | _ActiveSignin = await _decide(app, agent, method, timeout_s=timeout_s)
        elif method is not None and method != active.method:
            raise RutherfordError(
                ErrorCode.INVALID_INPUT,
                f"a sign-in to {agent} with {active.method!r} is already running (job {active.job_id}); "
                f"finish or cancel_job it before signing in with {method!r}",
            )
        else:
            outcome = active
    if isinstance(outcome, str):
        return outcome
    # A joining caller gets the same bounded wait for the link as the one that started the sign-in.
    return await _await_link(app, agent, outcome, wait_s=wait_s, joined=outcome is active)


async def _decide(app: AppContext, agent: str, method: str | None, *, timeout_s: float) -> str | _ActiveSignin:
    """Check the agent's state and either answer now (an envelope) or submit the sign-in job (its entry)."""
    descriptor = app.descriptors.get(agent)
    state, methods, detail = await _current_state(descriptor)
    if state == "authenticated":
        # An explicit check found the agent opening sessions (perhaps signed in through its own CLI): lift a bench
        # it picked up while signed out, the same decision a successful sign-in job makes.
        app.delegation.clear_cooldown(agent)
    if state != "auth_required":
        # Signed in already, not installed, or broken in a way a sign-in cannot fix: report it and stop. A
        # method passed here is ignored on purpose -- there is nothing to sign in to, and an already-signed-in
        # agent must not be pushed through a second consent screen.
        return tool_success({"agent": agent, "status": state, "detail": detail})

    payload_methods = [m.as_payload() for m in methods]
    if method is None:
        return tool_success(
            {
                "agent": agent,
                "status": "method_required",
                "detail": detail,
                "methods": payload_methods,
                "next": f"call login agent={agent} method=<id> with one of these method ids",
            }
        )

    chosen = next((m for m in methods if m.id == method), None)
    if chosen is None:
        known = ", ".join(m.id for m in methods) or "(none)"
        raise RutherfordError(
            ErrorCode.INVALID_INPUT, f"{agent} does not offer sign-in method {method!r}; it offers: {known}"
        )
    if chosen.kind == "env_var":
        return tool_success(_env_var_answer(agent, chosen))
    if chosen.kind == "terminal":
        return tool_success(_terminal_answer(agent, descriptor, chosen))
    return await _submit_signin(app, descriptor, chosen, timeout_s=timeout_s)


async def _current_state(descriptor: AgentDescriptor) -> tuple[str, list[AuthMethodInfo], str]:
    """Open (and close) a session without signing in: ``(state, advertised methods, detail)``.

    ``state`` is ``authenticated`` / ``auth_required`` / ``not_installed`` / ``handshake_failed``. The
    advertised methods are read off the session even when it failed, since that is the case they matter in.
    """
    # Not a TemporaryDirectory: its cleanup would run when close() returns, which can be before the agent is
    # gone, and Windows refuses to delete a live process's working directory. Removed once it really is gone.
    cwd = Path(tempfile.mkdtemp(prefix="rutherford-acp-login-"))
    session = ACPSession(descriptor, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(cwd))
    try:
        try:
            await session.open()
        except ACPHandshakeError as exc:  # open() has already closed the session on this path
            if exc.code is ErrorCode.AUTH_REQUIRED:
                return "auth_required", session.auth_methods, exc.message
            if exc.code is ErrorCode.ACP_SPAWN_FAILED:
                return "not_installed", [], exc.message
            return "handshake_failed", session.auth_methods, exc.message
        with contextlib.suppress(Exception):
            await session.close()
    finally:
        session.when_closed(lambda: shutil.rmtree(cwd, ignore_errors=True))
    return (
        "authenticated",
        session.auth_methods,
        f"a session with {descriptor.id} opens, so it needs no ACP sign-in; a turn may still need a provider key "
        f"or entitlement of its own (run doctor agent={descriptor.id} to check a full turn)",
    )


#: Labels a ``run_in_terminal`` answer: its arguments and environment are the agent's, not Rutherford's.
_COMMAND_NOTE = (
    "the launch command is this agent's configured command; terminal_args and env were advertised by the agent "
    "and not checked by Rutherford. Review them before running the command."
)


def _env_var_answer(agent: str, method: AuthMethodInfo) -> dict[str, Any]:
    """What to set for an ``env_var`` method. Rutherford never asks for, reads, or stores the value."""
    names = [var.name for var in method.env_vars]
    answer: dict[str, Any] = {
        "agent": agent,
        "status": "configure_env",
        "method": method.as_payload(),
        "next": (
            f"set {', '.join(names) or 'the listed variables'} in the environment Rutherford runs in, or under "
            f"[agents.{agent}.env] in config, then re-run doctor agent={agent}"
        ),
    }
    if method.link:
        answer["link"] = method.link
        answer["link_note"] = (
            "the link was advertised by the agent and passed Rutherford's https and no-credential checks; "
            "Rutherford did not open it"
        )
    return answer


def _terminal_answer(agent: str, descriptor: AgentDescriptor, method: AuthMethodInfo) -> dict[str, Any]:
    """The command a user runs themselves for a ``terminal`` method (an MCP server has no terminal to lend)."""
    argv = [*descriptor.command, *method.terminal_args]
    return {
        "agent": agent,
        "status": "run_in_terminal",
        "method": method.as_payload(),
        "command": argv,
        "env": method.terminal_env,
        "command_note": _COMMAND_NOTE,
        "next": f"run the command in an interactive terminal, finish the sign-in, then re-run doctor agent={agent}",
    }


@dataclass(frozen=True, slots=True)
class _ActiveSignin:
    """A sign-in job in flight for one agent, so a retried ``login`` joins it instead of starting another."""

    job_id: str
    method: str
    timeout_s: float
    session: ACPSession
    #: Set once the job's agent has been killed and reaped (not merely once ``close`` returned), whatever way the
    #: job ended.
    torn_down: asyncio.Event
    #: Set when the job body begins, i.e. once an agent may exist. Owned by the entry rather than read off the
    #: job record, because the record can be evicted from the store while the agent is still closing.
    started: asyncio.Event


#: The sign-in in flight per agent, per job store. Keyed weakly on the store so a discarded app context (every
#: test builds its own) takes its entries with it.
_ACTIVE: weakref.WeakKeyDictionary[JobStore, dict[str, _ActiveSignin]] = weakref.WeakKeyDictionary()
#: One lock per agent, per job store. A ``login`` holds it from the in-flight check through the state check and
#: the job submit, so two concurrent calls cannot both see "nothing running" and each launch a sign-in.
_LOCKS: weakref.WeakKeyDictionary[JobStore, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()
#: How long a new ``login`` waits for a just-finished (e.g. cancelled) sign-in to tear its agent down before it
#: launches another. Above the session close budget, so a normal teardown always fits.
_TEARDOWN_WAIT_S = 20.0


async def _job_or_none(app: AppContext, job_id: str) -> JobRecord | None:
    """The job record, or ``None`` once the store no longer holds it (evicted after its TTL)."""
    try:
        return await app.jobs.get(job_id)
    except RutherfordError:
        return None


def _agent_lock(app: AppContext, agent: str) -> asyncio.Lock:
    return _LOCKS.setdefault(app.jobs, {}).setdefault(agent, asyncio.Lock())


async def _active_signin(app: AppContext, agent: str) -> _ActiveSignin | None:
    """The unfinished sign-in job for ``agent``, or ``None``. Call with the agent's lock held.

    A finished entry is dropped, but only after its job has finished tearing down -- a sign-in cancelled a
    moment ago may still be closing its agent, and launching another then would put two sign-ins in flight.
    """
    active = _ACTIVE.get(app.jobs, {})
    entry = active.get(agent)
    if entry is None:
        return None
    record = await _job_or_none(app, entry.job_id)
    if record is not None and not record.is_finished:
        return entry
    # Finished, or evicted from the store: the record's lifecycle is not the agent's. If the job ever ran, an
    # agent may still be closing, so wait (bounded) for its teardown before freeing the slot. A job cancelled
    # before it ran never spawned anything and frees the slot at once.
    if entry.started.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(entry.torn_down.wait(), timeout=_TEARDOWN_WAIT_S)
    active.pop(agent, None)
    return None


def _pending(agent: str, entry: _ActiveSignin, *, wait_s: float, joined: bool) -> str:
    """The ``pending`` envelope for an in-flight sign-in, with the links the agent has printed so far.

    ``joined`` marks a caller that attached to a sign-in another call started, whose ``method`` and
    ``timeout_s`` (reported here) are the ones in effect.
    """
    urls = entry.session.signin_urls
    return tool_success(
        {
            "agent": agent,
            "status": "pending",
            "method": entry.method,
            "job_id": entry.job_id,
            "joined": joined,
            "signin_urls": urls,
            "timeout_s": entry.timeout_s,
            "note": _URL_NOTE
            if urls
            else (
                f"the agent printed no sign-in link within {wait_s:g}s; it may have opened a browser itself. Poll "
                "job_status / job_result with this job_id for the outcome."
            ),
        }
    )


def _finish_teardown(cwd: Path, torn_down: asyncio.Event, *, release: Callable[[], None] | None = None) -> None:
    """Remove the sign-in's working directory, mark the sign-in torn down, then free its slot (agent gone)."""
    try:
        shutil.rmtree(cwd, ignore_errors=True)
    finally:
        torn_down.set()
        if release is not None:
            release()


async def _submit_signin(
    app: AppContext, descriptor: AgentDescriptor, method: AuthMethodInfo, *, timeout_s: float
) -> _ActiveSignin:
    """Submit ``authenticate`` + a confirming ``session/new`` as a job and register it as the agent's sign-in.

    Called with the agent's lock held, so registration is atomic with the in-flight check that preceded it.
    """
    # Named here, CREATED inside the job: a job that never runs (a full job store, or a cancel while it is still
    # queued) then leaves no directory behind. ACPSession only resolves the path, so it need not exist yet.
    cwd = Path(tempfile.gettempdir()) / f"rutherford-acp-login-{uuid.uuid4().hex[:12]}"
    session = ACPSession(
        descriptor,
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=str(cwd),
        auth_method=method.id,
        auth_timeout_s=timeout_s,
    )

    async def _signin(_on_activity: object) -> str:
        started.set()  # before the try: from here on, the finally always arranges torn_down
        try:
            cwd.mkdir(parents=True, exist_ok=True)
            await session.open()  # authenticate, then session/new proves the credential actually works
        except ACPHandshakeError as exc:
            if exc.code is ErrorCode.AUTH_REQUIRED:
                # authenticate returned, yet the agent still refused a session: the sign-in did not take.
                raise RutherfordError(
                    ErrorCode.AUTH_FAILED,
                    f"signing in to {descriptor.id} with {method.id!r} completed, but the agent still refused a "
                    f"session: {exc.message}",
                ) from exc
            raise RutherfordError(exc.code, exc.message) from exc
        finally:
            try:
                with contextlib.suppress(Exception):
                    await session.close()
            finally:
                # Both run when the agent is really gone, not when close() returned: close() stops waiting after
                # its own budget while the kill and reap carry on. Before that, Windows refuses to delete a live
                # process's working directory, and a second sign-in must not start. Registered in a `finally`, so
                # a second cancel landing inside close() cannot skip them.
                session.when_closed(lambda: _finish_teardown(cwd, torn_down, release=_release_slot))
        # A seat benched for being signed out would otherwise sit out panels until its bench timed out.
        app.delegation.clear_cooldown(descriptor.id)
        return tool_success(
            {
                "agent": descriptor.id,
                "status": "authenticated",
                "method": method.id,
                "detail": f"signed in to {descriptor.id} with {method.id!r}; a session opened with the credential",
            }
        )

    torn_down, started = asyncio.Event(), asyncio.Event()
    registered: list[_ActiveSignin] = []  # filled right after submit, before the job body can run

    def _release_slot() -> None:
        """Drop this sign-in's entry once it is fully torn down, unless a newer sign-in has replaced it."""
        active = _ACTIVE.get(app.jobs, {})
        if registered and active.get(descriptor.id) is registered[0]:
            del active[descriptor.id]

    job_id = await app.jobs.submit(
        "login", _signin, summary=make_summary("login", target=f"{descriptor.id} ({method.id})")
    )
    entry = _ActiveSignin(
        job_id=job_id, method=method.id, timeout_s=timeout_s, session=session, torn_down=torn_down, started=started
    )
    _ACTIVE.setdefault(app.jobs, {})[descriptor.id] = entry
    registered.append(entry)
    return entry


async def _await_link(app: AppContext, agent: str, entry: _ActiveSignin, *, wait_s: float, joined: bool = False) -> str:
    """Wait up to ``wait_s`` for the sign-in's link or its finish; return ``pending`` or the finished outcome."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        record = await _job_or_none(app, entry.job_id)
        if record is None or record.is_finished or entry.session.signin_urls:
            break
        await asyncio.sleep(_POLL_INTERVAL_S)

    record = await _job_or_none(app, entry.job_id)
    if record is None:
        raise RutherfordError(
            ErrorCode.JOB_NOT_FOUND,
            f"the sign-in job {entry.job_id} for {agent} is no longer tracked (job store reset or evicted); run "
            f"login agent={agent} again to check its state",
        )
    if record.is_finished:
        if record.result is not None:
            return record.result  # signed in before the wait ran out (e.g. the agent had a cached credential)
        if record.status is JobStatus.CANCELLED:
            # A normal user action (cancel_job while this call waited), not a fault: answer it as an outcome.
            return tool_success(
                {
                    "agent": agent,
                    "status": "cancelled",
                    "method": entry.method,
                    "job_id": entry.job_id,
                    "next": f"the sign-in was cancelled; call login agent={agent} method=<id> to start a new one",
                }
            )
        error = record.error
        raise RutherfordError(
            error.code if error is not None else ErrorCode.INTERNAL,
            error.message if error is not None else f"signing in to {agent} did not complete",
        )
    return _pending(agent, entry, wait_s=wait_s, joined=joined)
