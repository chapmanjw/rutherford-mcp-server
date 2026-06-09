# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the structured logging layer (``runtime/logging``) and its instrumentation."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.models import DelegationRequest, ProcessResult, Target
from rutherford.runtime.logging import LOGGER_NAME, configure_logging, log_event
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from tests.fakes import FakeAdapter, FakeProcessRunner


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    # Restore the package logger to its pristine default after each test (handlers cleared, level
    # NOTSET, propagate=True) so configuring it here does not leak into the rest of the suite -- in
    # particular, leaving propagate=False would break caplog-based tests in other files.
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def _stream() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    return stream


def test_log_event_emits_json_with_fields_and_drops_none() -> None:
    stream = _stream()
    log_event("delegate", correlation_id="abc", cli="claude_code", ok=True, error_code=None)
    payload = json.loads(stream.getvalue().strip())
    assert payload["event"] == "delegate"
    assert payload["correlation_id"] == "abc"
    assert payload["cli"] == "claude_code"
    assert payload["ok"] is True
    assert "error_code" not in payload  # None-valued fields are dropped
    assert "ts" in payload


def test_log_format_off_silences() -> None:
    stream = io.StringIO()
    configure_logging("info", "off", stream=stream)
    log_event("delegate", cli="x")
    assert stream.getvalue() == ""


def test_level_below_threshold_is_suppressed() -> None:
    stream = _stream()  # info
    log_event("trace", level=logging.DEBUG, x=1)
    assert stream.getvalue() == ""


async def test_delegation_emits_a_delegate_event() -> None:
    stream = _stream()
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = DelegationService(AdapterRegistry([FakeAdapter("a")]), runner, RutherfordConfig(), load_roles())
    await service.delegate(DelegationRequest(target=Target(cli="a"), prompt="q"), correlation_id="cid-1")
    events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    delegate_events = [event for event in events if event["event"] == "delegate"]
    assert delegate_events, "no delegate event was logged"
    assert delegate_events[0]["correlation_id"] == "cid-1"
    assert delegate_events[0]["cli"] == "a"
    assert delegate_events[0]["ok"] is True
