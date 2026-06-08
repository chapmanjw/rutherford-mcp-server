# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Fixtures for the integration suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rutherford.context import AppContext, build_app_context


@pytest.fixture(scope="session")
def real_app() -> Iterator[AppContext]:
    """A real application context: the asyncio process runner and the full adapter registry.

    Config is discovered from disk and ``RUTHERFORD_CONFIG`` (not hard-coded empty), so a contributor
    can set a per-adapter ``default_model`` -- required for the local adapters (Ollama, LM Studio),
    which have no built-in default -- and have the no-model delegation tests use it.
    """
    yield build_app_context()
