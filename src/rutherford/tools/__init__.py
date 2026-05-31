# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tool implementations.

Each tool is a thin function: it parses input, calls a service, and returns the TOON-encoded
envelope. The FastMCP layer in :mod:`rutherford.server` wraps these and maps a
:class:`~rutherford.domain.errors.RutherfordError` to an MCP tool error. Keeping the logic here
(not in the FastMCP wrappers) makes every tool testable without the transport.
"""
