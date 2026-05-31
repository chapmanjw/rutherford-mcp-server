# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Domain layer: models, enums, errors, and the stable error-code set.

This is the innermost layer. It has no dependency on adapters, the runtime, or FastMCP, so
the orchestration contract is defined independently of any concrete CLI or transport.
"""
