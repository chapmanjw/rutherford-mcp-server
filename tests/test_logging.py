# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the structured JSON logging seam."""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from rutherford.runtime.logging import LOGGER_NAME, configure_logging, log_event


class _BlockingStream:
    """Block the first write long enough to expose synchronous handler backpressure without hanging the suite."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True

    def write(self, text: str) -> int:
        """Hold only the first write; later writes remain fast after the regression probe."""
        if self._first:
            self._first = False
            self.entered.set()
            self.release.wait(timeout=2.0)
        return len(text)

    def flush(self) -> None:
        """Provide the stream method required by logging handlers."""


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


def test_default_stderr_backpressure_does_not_block_log_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _BlockingStream()
    monkeypatch.setattr(sys, "stderr", stream)
    configure_logging("info", "json")

    first = threading.Thread(target=log_event, args=("first",), daemon=True)
    first.start()
    assert stream.entered.wait(timeout=1.0)
    started = time.monotonic()
    for index in range(2048):
        log_event("queued", index=index)
    elapsed = time.monotonic() - started
    stream.release.set()
    first.join(timeout=1.0)

    assert elapsed < 0.5
    assert not first.is_alive()

    # * Wait for the WRITER thread itself, not for the queue to look empty. `Queue.get` removes the last
    # item BEFORE the write and flush run, so an empty queue is not a finished writer -- the thread can
    # still be executing measured lines after the test returns, which is what made the per-file coverage
    # floor move with machine load. Closing joins the worker, which is the real completion signal.
    handler = logging.getLogger(LOGGER_NAME).handlers[0]
    handler.close()
    worker = handler._worker  # type: ignore[attr-defined]
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "the background writer thread never finished"


def test_background_handler_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    configure_logging("info", "json")
    handler = logging.getLogger(LOGGER_NAME).handlers[0]

    handler.close()
    handler.close()


def test_background_handler_close_flushes_final_record_to_a_healthy_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    configure_logging("info", "json")
    log_event("final_lifecycle_record")
    handler = logging.getLogger(LOGGER_NAME).handlers[0]

    handler.close()

    assert '"event":"final_lifecycle_record"' in stream.getvalue()


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


def test_dropped_records_are_reported_not_lost_silently() -> None:
    """Saturation must leave a mark in the stream.

    Dropping under backpressure is correct -- blocking the event loop on a wedged sink is worse. Dropping
    SILENTLY is not: the queue fills exactly when something is stuck, which is the incident someone will
    later read these logs to understand. A gap with no marker reads as "nothing happened".
    """
    from rutherford.runtime import logging as rlogging

    class _Sink:
        def __init__(self) -> None:
            self.lines: list[str] = []
            self.blocked = threading.Event()

        def write(self, text: str) -> int:
            self.blocked.wait()  # hold the writer thread so the queue backs up
            self.lines.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    sink = _Sink()
    handler = rlogging._BackgroundStreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        # * Records carry JSON messages, as log_event produces in production, so "every line parses"
        # is a meaningful assertion about the stream rather than an artifact of the fixture.
        overflow = rlogging._LOG_QUEUE_SIZE + 50
        for i in range(overflow):
            message = json.dumps({"ts": 0.0, "event": "probe", "seq": i}, separators=(",", ":"))
            handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None))
        assert handler._dropped > 0, "the queue should have overflowed with the sink held"

        sink.blocked.set()  # let the writer drain
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not any("dropped" in line for line in sink.lines):
            time.sleep(0.02)

        # * Parse EVERY line: this stream is one JSON object per line by contract, and a plain-text
        # notice would make a shipper treat saturation as malformed input and drop it -- losing exactly
        # the signal that says why the surrounding records are missing.
        parsed = [json.loads(line) for line in sink.lines]
        notices = [obj for obj in parsed if obj.get("event") == "log_records_dropped"]
        assert notices, f"no drop notice reached the stream; got {len(sink.lines)} lines"
        assert notices[0]["count"] > 0
        assert isinstance(notices[0]["ts"], float)
        assert handler._dropped == 0  # settled once reported, so a later notice cannot double-count
    finally:
        sink.blocked.set()
        handler.close()
