# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Rutherford: an MCP server that orchestrates agentic coding agents over ACP.

Rutherford lets any MCP client delegate work to, and build consensus across, a crew of coding agents
driven through Zed's Agent Client Protocol (ACP). The orchestration core depends only on the agent
:class:`~rutherford.acp.descriptors.DescriptorRegistry` and the ACP session runtime; an agent is a small
descriptor (how to launch it as an ACP server), not a hand-written per-CLI adapter. (v3: ACP-native rebuild.)
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rutherford-mcp-server")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
