# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Local-only integration tests (real CLIs). Marked ``integration`` and deselected by default.

Run with ``pytest -m integration`` (or ``just test-integration``). Each CLI's tests skip
themselves unless opted in (``RUTHERFORD_IT_<CLI>=1``) and actually installed and authenticated.
See docs/integration-testing.md.
"""
