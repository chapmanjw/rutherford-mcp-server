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

import asyncio
import urllib.error
from collections.abc import Awaitable, Callable, Iterator

import pytest

from rutherford.services.jobs import JobRecord, JobStore

#: The signature of the ``drain_async_job`` fixture's helper: await a job to a terminal state.
DrainAsyncJob = Callable[[JobStore, str], Awaitable[JobRecord]]


@pytest.fixture(autouse=True)
def _no_live_local_backends(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse local-backend HTTP probes so the suite never touches a real Ollama / LM Studio."""
    # test_local_detect mocks urlopen itself; test_registry / test_discover fetch a local file:// registry
    # fixture (no network, no local backend), so they opt out of the blanket urlopen refusal too.
    if request.module.__name__.rsplit(".", 1)[-1] in ("test_local_detect", "test_registry", "test_discover"):
        yield
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

    This applies to integration tests too, so a developer's real ``~/.rutherford`` roles/panels never leak
    into one that builds an ``AppContext``. An integration test that drives a real agent needing
    credentials from the home dir (e.g. Grok reads ``~/.grok/auth``) restores ONLY that home for itself via a
    local fixture (see ``tests/integration/test_grok.py``); it does not blanket-disable this isolation.
    """
    home = tmp_path_factory.mktemp("hermetic-home")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("RUTHERFORD_CONFIG_DIR", raising=False)
    # The project scope keys off the process cwd; the repo root has no .rutherford/, so it stays empty.


@pytest.fixture
def drain_async_job() -> DrainAsyncJob:
    """Provide a helper that awaits a background job to a terminal state -- the async-test cleanup contract.

    A fire-and-forget async job (``mode="async"``) runs a JobStore task that drives a real fake-agent
    subprocess. A test that asserts the immediate ``job_id`` and then returns leaves that task pending --
    and pytest-asyncio's loop teardown (``_cancel_all_tasks`` -> ``run_until_complete``, no timeout) then
    HANGS forever on the Windows ProactorEventLoop under Python 3.11 / 3.12, trying to cancel a subprocess
    transport that is mid-spawn (a mid-spawn subprocess CANNOT be cancelled cleanly there; 3.13's reworked
    loop teardown is why CI only hung on the older interpreters). Letting the job FINISH -- the fast fake
    agent answers in well under a second -- means its subprocess exits on its own, so nothing un-cancellable
    is left for the loop close. Every async-job test must drain the job it submits.
    """

    async def _drain(jobs: JobStore, job_id: str, *, timeout_s: float = 30.0) -> JobRecord:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            record = await jobs.get(job_id)
            if record.is_finished:
                return record
            if asyncio.get_running_loop().time() >= deadline:  # pragma: no cover - a fast fake never waits this long
                raise AssertionError(f"async job {job_id} did not finish within {timeout_s:.0f}s")
            await asyncio.sleep(0.02)

    return _drain
