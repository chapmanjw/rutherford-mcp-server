# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared test fixtures: keep the suite hermetic and fast.

``auto_detect_local_models`` is on by default, so without this every ``build_app_context`` /
``build_registry`` would probe a live Ollama / LM Studio over HTTP at registry-build time -- slow and
dependent on whatever happens to be running on the machine. This autouse fixture makes the local-runtime
probe fail fast (so detection contributes no agents) for every test, EXCEPT ``test_local_detect``, which
exercises the real detector with its own ``urlopen`` fakes.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_live_local_backends(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse local-backend HTTP probes so the suite never touches a real Ollama / LM Studio."""
    if request.module.__name__.rsplit(".", 1)[-1] == "test_local_detect":
        yield  # this module mocks urlopen itself to test detection
        return

    def _refuse(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("local-backend probing is disabled in tests")

    monkeypatch.setattr("urllib.request.urlopen", _refuse)
    yield
