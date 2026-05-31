# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Fixtures for the integration suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context


@pytest.fixture(scope="session")
def real_app() -> Iterator[AppContext]:
    """A real application context: the asyncio process runner and the full adapter registry."""
    yield build_app_context(config=RutherfordConfig())
