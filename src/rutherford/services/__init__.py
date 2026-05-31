# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Services: the orchestration layer (delegation, consensus, jobs, roles).

Services depend only on the abstract ``CLIAdapter`` and ``ProcessRunner`` interfaces (by
constructor injection), never on a concrete CLI, so the whole layer is testable with fakes.
"""
