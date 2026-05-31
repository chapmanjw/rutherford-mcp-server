# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Rutherford: an MCP server that orchestrates agentic coding CLIs.

Rutherford lets any MCP client delegate work to, and build consensus across, a crew of
terminal coding agents (Claude Code, Codex, Antigravity, Kiro, OpenCode, Goose). The
orchestration core depends only on the abstract :class:`~rutherford.adapters.base.CLIAdapter`
and :class:`~rutherford.runtime.process.ProcessRunner` interfaces; every CLI-specific detail
lives behind an adapter.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rutherford-mcp-server")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
