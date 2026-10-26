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
from typing import Any

from ..acp.auth import AuthMethodInfo
from ..acp.descriptors import AgentDescriptor
from ..acp.permission import PermissionPolicy
from ..acp.session import ACPHandshakeError, ACPSession
from ..context import AppContext, tool_success
from ..domain.enums import SafetyMode
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
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

    Without ``method``: opens a session (no prompt). Already signed in -> ``status=authenticated``. Not signed
    in -> ``status=method_required`` with the advertised ``methods``. With ``method`` on an ``agent``-kind
    method: starts ``authenticate`` in a background job, waits up to ``wait_s`` for the agent to print a
    sign-in link or finish, and returns ``status=pending`` (``job_id`` + ``signin_urls``) or the finished
    outcome. ``timeout_s`` bounds the whole sign-in. ``env_var`` / ``terminal`` methods return what the user
    has to set or run instead (``status=configure_env`` / ``run_in_terminal``).
    """
    ensure_known_agent(app.descriptors, agent)
    if not 0 <= wait_s <= _MAX_WAIT_S:
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"wait_s must be between 0 and {_MAX_WAIT_S:g}")
    if not 0 < timeout_s <= _MAX_TIMEOUT_S:
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"timeout_s must be above 0 and at most {_MAX_TIMEOUT_S:g}")
    descriptor = app.descriptors.get(agent)

    state, methods, detail = await _current_state(descriptor)
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
    return await _start_signin(app, descriptor, chosen, wait_s=wait_s, timeout_s=timeout_s)


async def _current_state(descriptor: AgentDescriptor) -> tuple[str, list[AuthMethodInfo], str]:
    """Open (and close) a session without signing in: ``(state, advertised methods, detail)``.

    ``state`` is ``authenticated`` / ``auth_required`` / ``not_installed`` / ``handshake_failed``. The
    advertised methods are read off the session even when it failed, since that is the case they matter in.
    """
    with tempfile.TemporaryDirectory(prefix="rutherford-acp-login-", ignore_cleanup_errors=True) as cwd:
        session = ACPSession(descriptor, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=cwd)
        try:
            await session.open()
        except ACPHandshakeError as exc:
            if exc.code is ErrorCode.AUTH_REQUIRED:
                return "auth_required", session.auth_methods, exc.message
            if exc.code is ErrorCode.ACP_SPAWN_FAILED:
                return "not_installed", [], exc.message
            return "handshake_failed", session.auth_methods, exc.message
        with contextlib.suppress(Exception):
            await session.close()
    return "authenticated", session.auth_methods, f"{descriptor.id} is already signed in (a session opened)"


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
        "next": f"run the command in an interactive terminal, finish the sign-in, then re-run doctor agent={agent}",
    }


async def _start_signin(
    app: AppContext, descriptor: AgentDescriptor, method: AuthMethodInfo, *, wait_s: float, timeout_s: float
) -> str:
    """Run ``authenticate`` + a confirming ``session/new`` as a job; return its link, or its result if quick."""
    cwd = tempfile.mkdtemp(prefix="rutherford-acp-login-")
    session = ACPSession(
        descriptor,
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=cwd,
        auth_method=method.id,
        auth_timeout_s=timeout_s,
    )

    async def _signin(_on_activity: object) -> str:
        try:
            await session.open()  # authenticate, then session/new proves the credential actually works
        except ACPHandshakeError as exc:
            raise RutherfordError(exc.code, exc.message) from exc
        finally:
            with contextlib.suppress(Exception):
                await session.close()
            shutil.rmtree(cwd, ignore_errors=True)
        return tool_success(
            {
                "agent": descriptor.id,
                "status": "authenticated",
                "method": method.id,
                "detail": f"signed in to {descriptor.id} with {method.id!r}; a session opened with the credential",
            }
        )

    job_id = await app.jobs.submit(
        "login", _signin, summary=make_summary("login", target=f"{descriptor.id} ({method.id})")
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        record = await app.jobs.get(job_id)
        if record.is_finished or session.signin_urls:
            break
        await asyncio.sleep(_POLL_INTERVAL_S)

    record = await app.jobs.get(job_id)
    if record.is_finished:
        if record.result is not None:
            return record.result  # signed in before the wait ran out (e.g. the agent had a cached credential)
        error = record.error
        raise RutherfordError(
            error.code if error is not None else ErrorCode.INTERNAL,
            error.message if error is not None else f"signing in to {descriptor.id} did not complete",
        )
    urls = session.signin_urls
    return tool_success(
        {
            "agent": descriptor.id,
            "status": "pending",
            "method": method.id,
            "job_id": job_id,
            "signin_urls": urls,
            "timeout_s": timeout_s,
            "note": _URL_NOTE
            if urls
            else (
                f"the agent printed no sign-in link within {wait_s:g}s; it may have opened a browser itself. Poll "
                "job_status / job_result with this job_id for the outcome."
            ),
        }
    )
