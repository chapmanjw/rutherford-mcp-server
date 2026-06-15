# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration test: connect to the real ``grok agent stdio`` agent (local only, run with -m integration).

A CONNECTION test, not a turn test. Grok is ACP-native (xAI), but a *completed* turn needs a SuperGrok
subscription -- without it the model call returns ``403 SuperGrok Heavy subscription required`` (which
surfaces only in grok's own stderr, not the structured ACP error). So this verifies what works regardless of
entitlement: Rutherford can spawn Grok, complete the ACP handshake, open a session, and read its advertised
models (communicate + configure). It then confirms a full turn is at least ATTEMPTED past spawn + handshake
and handled as a structured result (``ok`` if entitled, else a generic turn ``error``), without claiming the
specific 403. Skips when the grok CLI is not installed.

Grok reads its credentials from the real home (``~/.grok/auth``), which the hermetic-home autouse fixture in
``tests/conftest.py`` clobbers -- so these tests restore the real ``USERPROFILE`` / ``HOME`` for themselves
(captured at import, before that fixture runs) via ``_real_agent_home``. They drive ``probe_connection`` /
``probe_agent`` directly (no ``AppContext`` with role/panel scopes), so restoring the home only reaches the
agent subprocess, never Rutherford's own ``~/.rutherford`` config -- no role/panel leak.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from rutherford.acp.conformance import ConformanceReport, ConnectionReport, probe_agent, probe_connection
from rutherford.acp.descriptors import AgentDescriptor, default_registry

pytestmark = pytest.mark.integration

_GROK_INSTALLED = shutil.which("grok") is not None
#: Captured at import (before the hermetic-home autouse fixture runs) so the agent can find ~/.grok/auth.
_REAL_HOME = {key: os.environ[key] for key in ("USERPROFILE", "HOME") if key in os.environ}


@pytest.fixture
def _real_agent_home(_isolate_config_scopes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real home for the agent subprocess (Grok's ``~/.grok/auth``).

    Depends on ``_isolate_config_scopes`` so it runs AFTER that autouse fixture has pointed the home at a tmp
    dir, then resets it to the real values. Scoped to a test that requests it, so no other test's hermetic
    isolation is affected.
    """
    for key, value in _REAL_HOME.items():
        monkeypatch.setenv(key, value)


async def _connect_with_retry(grok: AgentDescriptor, *, attempts: int = 3) -> ConnectionReport:
    """Probe Grok's connection, retrying a transient ``Authentication required``.

    Grok connects reliably, but rapid back-to-back ``grok agent stdio`` spawns can transiently fail the
    handshake with "Authentication required" (an xAI auth-refresh race, not a Rutherford fault) -- spacing the
    attempts clears it. Returns the last report so a genuine, persistent failure still surfaces.
    """
    report = await probe_connection(grok, timeout_s=90.0)
    for _ in range(attempts - 1):
        if report.status == "reachable":
            return report
        await asyncio.sleep(3)
        report = await probe_connection(grok, timeout_s=90.0)
    return report


async def _turn_with_retry(grok: AgentDescriptor, *, attempts: int = 3) -> ConformanceReport:
    """Run a full probe turn, retrying only the transient handshake auth race (not a real turn outcome)."""
    report = await probe_agent(grok, timeout_s=90.0)
    for _ in range(attempts - 1):
        if report.status != "handshake_failed":
            return report
        await asyncio.sleep(3)
        report = await probe_agent(grok, timeout_s=90.0)
    return report


@pytest.mark.skipif(not _GROK_INSTALLED, reason="the grok CLI is not installed")
async def test_grok_connects_and_advertises_models(_real_agent_home: None) -> None:
    """Spawn + handshake + new_session succeed (no prompt) -- proving Rutherford can talk to and configure Grok.

    Works WITHOUT a SuperGrok subscription: the handshake and session open precede any model call. The session
    advertises selectable models (e.g. ``grok-build``), which is the ``--model`` configuration surface.
    """
    grok = default_registry().get("grok")
    report = await _connect_with_retry(grok)
    assert report.status == "reachable", f"grok did not connect: {report.detail}"
    assert report.connected is True and report.session_id is not None
    assert report.models, "grok advertised no selectable models at open"


@pytest.mark.skipif(not _GROK_INSTALLED, reason="the grok CLI is not installed")
async def test_grok_turn_is_attempted_past_the_handshake(_real_agent_home: None) -> None:
    """After a verified connection, a full turn runs PAST spawn + handshake and returns a STRUCTURED result.

    The outcome depends on the account: ``ok`` if entitled (it answered), else a turn ``error``. NB: the
    structured error detail is generic ("ACP turn for grok failed: Internal error") -- the actual
    ``403 SuperGrok Heavy subscription required`` surfaces only in grok's own stderr, NOT the ACP error -- so
    this asserts the turn was attempted and cleanly handled (NOT ``not_installed``, and NOT ``handshake_failed``
    after the auth-race retry, i.e. it got past spawn + handshake), not the specific 403. Grok being wired up
    end to end is what this proves; the 403 itself is observed manually via `doctor`.
    """
    grok = default_registry().get("grok")
    connect = await _connect_with_retry(grok)
    assert connect.status == "reachable", f"grok did not connect: {connect.detail}"
    report = await _turn_with_retry(grok)
    assert report.installed is True
    # ok | error | no_answer all mean the turn ran past a successful handshake; not_installed / handshake_failed
    # (the latter only if the auth-race retry never cleared) would mean it never got that far -- a regression.
    assert report.status in ("ok", "error", "no_answer"), f"turn did not get past the handshake: {report.detail}"
