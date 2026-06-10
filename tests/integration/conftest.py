# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Fixtures for the integration suite."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from rutherford.context import AppContext, build_app_context

from .helpers import CLI_ENV, available_clis


@pytest.fixture(scope="session")
def real_app() -> Iterator[AppContext]:
    """A real application context: the asyncio process runner and the full adapter registry.

    Config is discovered from disk and ``RUTHERFORD_CONFIG`` (not hard-coded empty), so a contributor
    can set a per-adapter ``default_model`` -- required for the local adapters (Ollama, LM Studio),
    which have no built-in default -- and have the no-model delegation tests use it.
    """
    yield build_app_context()


@pytest.fixture(scope="session", autouse=True)
def _at_least_one_cli_or_fail(real_app: AppContext) -> None:
    """FAIL (not skip) an integration run in which no CLI is opted in, installed, and authenticated.

    Every test in this suite skips gracefully per-CLI, which is right for one missing CLI but wrong
    in aggregate: a misconfigured environment used to produce an all-skipped GREEN run -- a live
    "verification" that verified nothing. Running ``pytest -m integration`` is an explicit request
    to test real CLIs, so zero available CLIs is a failure of the run, not a skip.
    ``RUTHERFORD_IT_ALLOW_EMPTY=1`` is the explicit escape hatch (collect-only checks, CI dry runs).
    """
    if os.environ.get("RUTHERFORD_IT_ALLOW_EMPTY", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if not available_clis(real_app):
        names = ", ".join(sorted(CLI_ENV.values()))
        pytest.fail(
            "the integration suite ran with ZERO CLIs opted in -- every test would skip and the run "
            f"would falsely report green. Set at least one of: {names} (with that CLI installed and "
            "authenticated), or set RUTHERFORD_IT_ALLOW_EMPTY=1 to permit an empty run explicitly.",
            pytrace=False,
        )
