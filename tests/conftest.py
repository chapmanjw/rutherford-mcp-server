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


@pytest.fixture(autouse=True)
def _isolate_config_scopes(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the on-disk config scopes (roles + panels) at an EMPTY tmp home so the suite is hermetic.

    Roles and panels are discovered under ``~/.rutherford`` / ``<cwd>/.rutherford`` / ``$RUTHERFORD_CONFIG_DIR``
    (see :mod:`rutherford.config.locations`). The role store loads those scopes eagerly at
    ``build_app_context``, so without this a developer's real ``~/.rutherford/roles`` would override a built-in
    mid-test and a real ``panels.toon`` would leak in. This anchors the user scope at a fresh empty dir and
    clears ``RUTHERFORD_CONFIG_DIR`` for every test. A test that drives its own scopes passes ``env`` / ``cwd``
    to ``RoleStore`` / ``load_panels`` directly -- the injected mapping wins over the process env, so this
    fixture never interferes with those explicit-scope tests.
    """
    home = tmp_path_factory.mktemp("hermetic-home")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RUTHERFORD_CONFIG_DIR", raising=False)
    # The project scope keys off the process cwd; the repo root has no .rutherford/, so it stays empty.
