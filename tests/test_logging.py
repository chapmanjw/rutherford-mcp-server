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
    """Leave the package logger silent after each test, and hand the ROOT logger back exactly as found.

    ``configure_logging`` now also owns a handler on root, so silencing the package logger is no longer
    enough to leave the process as it was: without the snapshot below, this module would hand every later
    test file a root logger it did not ask for, and the tests here that clear ``logging.root.handlers`` to
    observe the ``basicConfig`` interaction would destroy pytest's own capture handlers on the way out.
    """
    from rutherford.runtime import logging as rlogging

    root_handlers = list(logging.root.handlers)
    root_level = logging.root.level
    yield
    configure_logging("info", "off")
    # Slice assignment rather than removing what we added: the tests below clear root wholesale, so the
    # only safe restore is to put back exactly the list that was there before.
    logging.root.handlers[:] = root_handlers
    logging.root.setLevel(root_level)
    rlogging._root_handler = None


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


def test_configure_logging_preempts_the_basicconfig_stderr_handler() -> None:
    """Root must never be left handler-less, or the first library error installs a synchronous writer.

    ``logging.exception`` at module level -- how the ACP SDK reports handler failures, since it defines no
    logger of its own -- runs ``basicConfig()`` whenever root has no handlers. That installs a plain
    ``StreamHandler(sys.stderr)`` at NOTSET and leaves it there for the process's life, so every later
    traceback is written synchronously from the event loop thread. Owning a root handler is the only thing
    that stops it, which makes this the load-bearing assertion of the whole arrangement.
    """
    logging.root.handlers[:] = []
    configure_logging("info", "json", stream=io.StringIO())
    installed = list(logging.root.handlers)

    logging.error("a library reporting through the root logger")

    assert logging.root.handlers == installed, "basicConfig() slipped a synchronous stderr handler onto root"


def test_off_format_still_holds_a_root_handler() -> None:
    """Silenced means Rutherford emits nothing, not that a library may install a stderr writer behind us."""
    logging.root.handlers[:] = []
    configure_logging("info", "off")
    assert len(logging.root.handlers) == 1
    assert isinstance(logging.root.handlers[0], logging.NullHandler)

    logging.error("a library reporting through the root logger")

    assert len(logging.root.handlers) == 1


def test_sdk_tracebacks_are_wrapped_into_one_json_line() -> None:
    """An SDK record reaches the configured sink as a single JSON object, traceback and all.

    A raw ``levelname:name:message`` header plus a multi-line traceback would break the one-object-per-line
    contract this stream is read under, so the failure a reader most needs -- an ACP callback blowing up
    mid-turn -- would be the one their shipper discards as malformed input.
    """
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)

    try:
        raise PermissionError(13, "Permission denied", "/sandbox/wt-1/src/secret.py")
    except PermissionError as exc:  # emulate the SDK's request dispatch on 0.12
        logging.exception("Unhandled error while handling request method=%s", "fs/write_text_file", exc_info=exc)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, f"the traceback spilled across {len(lines)} lines: {lines}"
    record = json.loads(lines[0])
    assert record["event"] == "foreign_log"
    assert record["logger"] == "root" and record["level"] == "ERROR"
    assert "fs/write_text_file" in record["message"]
    assert "PermissionError" in record["exc"]


def test_first_party_child_loggers_are_not_labelled_as_foreign() -> None:
    """A record from ``rutherford.<something>`` is ours, and the envelope has to say so.

    ``log_event`` is the only emitter on the bare package logger; every other module in this package logs
    through ``getLogger(__name__)``, which yields ``rutherford.server``, ``rutherford.services.consensus``,
    ``rutherford.acp.sandbox`` and the rest. Keying the origin test on an exact name match therefore labels
    almost all of this project's own diagnostics as records that came from somewhere else, and an operator
    reading ``foreign_log`` reasonably concludes the incident happened in a dependency. The verbatim
    pass-through still keys on the exact name -- that is where ``log_event`` puts an already-serialized JSON
    object, and the one-object-per-line contract depends on nothing else being waved through -- so the origin
    split lives in the envelope's event name instead.
    """
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)

    logging.getLogger("rutherford.server").error("the stdio entrypoint could not bind")

    record = json.loads(stream.getvalue().splitlines()[0])
    assert record["event"] == "log", "a first-party sub-logger was reported as a record from elsewhere"
    assert record["logger"] == "rutherford.server"
    assert record["message"] == "the stdio entrypoint could not bind"


def test_a_name_merely_prefixed_with_the_package_name_is_still_foreign() -> None:
    """``rutherfordctl`` is somebody else's logger; only a dotted CHILD of the package logger is ours.

    A bare ``startswith(LOGGER_NAME)`` would adopt any third-party logger whose name happens to begin with
    the same nine letters, which is how an origin label stops meaning anything. The separator is the whole
    test: ``rutherford.`` is the package's namespace, ``rutherford`` as a prefix is a coincidence.
    """
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)

    logging.getLogger("rutherfordctl").error("a differently-named package reporting a failure")

    record = json.loads(stream.getvalue().splitlines()[0])
    assert record["event"] == "foreign_log"
    assert record["logger"] == "rutherfordctl"


def test_log_event_records_are_not_double_emitted_through_root() -> None:
    """The package logger and root share one handler instance, so propagation would emit each record twice."""
    stream = io.StringIO()
    configure_logging("info", "json", stream=stream)
    log_event("delegation_done", cli="goose")
    assert stream.getvalue().count('"event":"delegation_done"') == 1


def test_reconfiguring_leaves_exactly_one_root_handler() -> None:
    """Repeated configuration must not stack writers on root; each one owns a queue and a daemon thread."""
    logging.root.handlers[:] = []
    for _ in range(3):
        configure_logging("info", "json", stream=io.StringIO())
    assert len(logging.root.handlers) == 1


def test_switching_off_to_json_retires_the_silencing_root_handler() -> None:
    """The 'off' path installs a DIFFERENT root handler than the package one, so retiring it needs its own step."""
    logging.root.handlers[:] = []
    configure_logging("info", "off")
    silencer = logging.root.handlers[0]

    configure_logging("info", "json", stream=io.StringIO())

    assert silencer not in logging.root.handlers
    assert len(logging.root.handlers) == 1


def test_foreign_root_handlers_are_left_alone() -> None:
    """A handler somebody else put on root is not ours to remove; we only ever retire our own."""
    logging.root.handlers[:] = []
    foreign = logging.NullHandler()
    logging.root.addHandler(foreign)

    configure_logging("info", "json", stream=io.StringIO())

    assert foreign in logging.root.handlers


def test_sdk_tracebacks_do_not_block_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of SDK tracebacks against a wedged stderr must not stall the thread that emitted them.

    The package logger's own backpressure is covered above; this is the same guarantee for records that
    arrive on ROOT, which is where every ACP SDK failure lands and where, unrouted, a ``basicConfig`` handler
    would write straight through from the event loop thread.
    """
    logging.root.handlers[:] = []
    stream = _BlockingStream()
    monkeypatch.setattr(sys, "stderr", stream)
    configure_logging("info", "json")

    first = threading.Thread(target=log_event, args=("first",), daemon=True)
    first.start()
    assert stream.entered.wait(timeout=1.0)
    started = time.monotonic()
    for index in range(64):
        try:
            raise RuntimeError(f"stream observer {index} failed")
        except RuntimeError as exc:
            logging.exception("Stream observer failed", exc_info=exc)
    elapsed = time.monotonic() - started
    stream.release.set()
    first.join(timeout=1.0)

    assert elapsed < 0.5

    handler = logging.getLogger(LOGGER_NAME).handlers[0]
    handler.close()
    worker = handler._worker  # type: ignore[attr-defined]
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "the background writer thread never finished"
