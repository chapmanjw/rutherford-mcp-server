# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``analyze`` tool: on-demand analysis over the kept run corpus (F3 cross-run).

Today it serves one report -- ``historical_agreement`` -- the read-only, across-panel companion to the
within-panel F3 vote-math. It never runs on a live panel and never reshapes a vote; calling it IS the opt-in.
"""

from __future__ import annotations

import asyncio

from ..context import AppContext, tool_error, tool_success
from ..domain.error_codes import ErrorCode
from ..services.analysis import HistoricalAgreementService

#: The analysis reports this tool can produce. One today; a closed set so an unknown report fails fast.
_REPORTS = ("historical_agreement",)


async def analyze_tool(app: AppContext, *, report: str = "historical_agreement") -> str:
    """Run a read-only analysis over the kept run corpus and return its structured report.

    ``report`` selects the analysis; ``historical_agreement`` (the default and only report today) scans the kept
    consensus panels and reports how often two DISTINCT model lineages reached the same verdict when they
    co-voted -- an OBSERVATIONAL signal for a human's roster choice, never a vote discount. The corpus is the
    runs the caller chose to keep (persist=true / default_persistence=job); an empty corpus returns an empty
    report whose notes explain how to build one. Reads the jobs tree off-thread (file I/O); never writes.
    """
    if report not in _REPORTS:
        return tool_error(
            ErrorCode.INVALID_INPUT,
            f"unknown report {report!r}; available: {', '.join(_REPORTS)}",
        )
    result = await asyncio.to_thread(HistoricalAgreementService(app.ledger).report)
    return tool_success(result)
