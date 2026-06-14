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

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .context import AppContext, build_app_context, error_payload_from, tool_error
from .domain.error_codes import ErrorCode
from .domain.errors import ConfigError, RutherfordError
from .runtime.logging import configure_logging
from .tools.capabilities import capabilities_tool
from .tools.delegate import delegate_tool

mcp: FastMCP = FastMCP(
    "rutherford",
    instructions=(
        "Rutherford orchestrates other agentic coding agents over the Agent Client Protocol (ACP). Use "
        "`delegate` to hand a task to one agent and `capabilities` to see which agents are available. "
        "Delegations default to the configured default_safety_mode (read_only out of the box); write and "
        "yolo are explicit opt-in behind a trusted workspace."
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
) -> str:
    """Delegate a task to one ACP agent and return its normalized result.

    `cli` is an agent id (see `capabilities`); `model` is optional (the agent's default otherwise).
    `safety_mode` is read_only | propose | write | yolo; when omitted, the configured `default_safety_mode`
    applies (read_only out of the box). write and yolo also need a trusted workspace (`trust_workspace=true`
    or a configured allowlist). `files` lists paths to put in scope.
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
        )
    )


@mcp.tool
async def capabilities() -> str:
    """List the ACP agents Rutherford can drive (id, display name, launch command, provider)."""
    return await _guarded(capabilities_tool(get_app()))


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
