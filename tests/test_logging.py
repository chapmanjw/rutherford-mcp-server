# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the structured logging layer (``runtime/logging``) and its instrumentation."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator

import pytest

from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.domain.enums import JobStatus
from rutherford.domain.models import DelegationRequest, DelegationResult, ProcessResult, Target
from rutherford.runtime.logging import LOGGER_NAME, configure_logging, log_event
from rutherford.services.delegation import DelegationService
from rutherford.services.jobs import JobService
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


async def test_failed_job_logs_error_type_not_the_exception_message() -> None:
    # A crashing body's exception message could carry prompt/secret content; the structured log must
    # record only the exception TYPE, never the message.
    stream = _stream()
    service = JobService()

    async def body(progress: object, activity: object, set_interim: object) -> DelegationResult:
        raise ValueError("SECRET prompt content that must not be logged")

    job = service.submit("delegate", body)
    for _ in range(500):
        if service.get(job.id).status is JobStatus.FAILED:
            break
        await asyncio.sleep(0.005)  # real time, not a zero-delay busy-yield (see test_jobs._wait_terminal)
    finished = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip() and json.loads(line).get("event") == "job_finished"
    ]
    assert finished and finished[0]["status"] == "failed"
    assert finished[0]["error_type"] == "ValueError"
    assert "SECRET" not in stream.getvalue()  # the exception message never reaches the logs
