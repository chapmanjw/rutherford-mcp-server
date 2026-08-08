# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Fixtures and bounds for the integration suite, which is the only one that drives real agents.

The unit suite is hermetic -- it talks to ``tests/fake_acp_agent.py`` and spawns nothing real -- so nothing
there hangs. These tests spawn actual agent processes and speak to them over stdio, which is exactly where
this project's nastiest bug lived: a child inheriting the live MCP pipe and freezing at zero CPU, waiting on
a read nobody would ever answer.

The tests already pass explicit ``timeout_s`` values into Rutherford, and Rutherford enforces them. That
covers a hang INSIDE a turn. It does not cover a hang before a turn starts, or in setup, or in the spawn
itself -- which is precisely the shape the deadlock had. This is the outer bound for those: a hung test dies
with a traceback pointing at the line, instead of the run sitting there looking busy.
"""

from __future__ import annotations

import pytest

#: Comfortably above the largest in-test budget (a 360s ``timeout_s``) plus spawn and handshake, so this
#: fires only when something is genuinely stuck rather than merely slow.
_INTEGRATION_TIMEOUT_S = 600


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Bound every integration test, without touching the hermetic unit suite.

    Applied here rather than in ``addopts`` on purpose: a global timeout would also wrap the unit tests,
    which do not need it and which run on nine CI cells of varying speed. A test that sets its own
    ``@pytest.mark.timeout`` keeps it -- an explicit local bound outranks this default.
    """
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_INTEGRATION_TIMEOUT_S))
