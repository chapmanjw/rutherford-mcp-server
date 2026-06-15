# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the structured JSON logging seam."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from rutherford.runtime.logging import configure_logging, log_event


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Leave the package logger silent after each test so a StringIO handler never leaks."""
    yield
    configure_logging("info", "off")


def test_json_logging_emits_event_and_drops_none_fields() -> None:
    stream = io.StringIO()
    configure_logging("debug", "json", stream=stream)
    log_event("delegation_done", cli="goose", duration_s=1.2, err=None)
    out = stream.getvalue()
    assert '"event":"delegation_done"' in out
    assert '"cli":"goose"' in out and '"duration_s":1.2' in out
    assert "err" not in out  # None-valued fields are dropped


def test_off_format_is_silent() -> None:
    stream = io.StringIO()
    configure_logging("info", "off", stream=stream)
    log_event("anything", x=1)
    assert stream.getvalue() == ""


def test_log_event_is_a_noop_below_the_configured_level() -> None:
    stream = io.StringIO()
    configure_logging("error", "json", stream=stream)
    log_event("debug_event", level=logging.DEBUG)  # below ERROR -> dropped, no guard needed at the call site
    assert stream.getvalue() == ""


def test_configure_logging_is_idempotent() -> None:
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    configure_logging("info", "json", stream=stream)  # re-config clears the prior handler, no duplicate lines
    log_event("once")
    assert stream.getvalue().count('"event":"once"') == 1
