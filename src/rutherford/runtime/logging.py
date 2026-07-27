# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Structured, correlation-id-keyed logging for diagnosability.

The server fans out to many CLIs and runs long background jobs, but until now correlation ids were
threaded and never emitted, so a failed panel left almost no trail. This module emits one JSON object
per significant lifecycle event (a delegation finishing, a job's state changing) to **stderr** --
stdout is the MCP protocol channel and must never be polluted -- keyed on the correlation id that
already flows through the services. It is deliberately small: no external telemetry backend, and no
prompt/response content is ever logged (only ids, adapter/model, safety mode, depth, duration, and
the error code). ``log_format = "off"`` silences it entirely.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import sys
import threading
import time
from typing import Any

#: The single logger the whole package emits through. Configured once at server startup.
LOGGER_NAME = "rutherford"

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_logger = logging.getLogger(LOGGER_NAME)

#: A blocked MCP host must bound memory as well as keep the event loop responsive.
_LOG_QUEUE_SIZE = 1024
#: Healthy sinks receive a brief bounded drain window; blocked sinks can stall only the daemon writer.
_LOG_CLOSE_TIMEOUT_S = 0.05
_STOP = object()


class _BackgroundStreamHandler(logging.Handler):
    """Write formatted records from a daemon thread without blocking the caller on stderr backpressure."""

    def __init__(self, stream: Any) -> None:
        super().__init__()
        self._stream = stream
        self._records: queue.Queue[str | object] = queue.Queue(maxsize=_LOG_QUEUE_SIZE)
        # * Dropping under saturation is the right call -- the alternative is blocking the event loop --
        # but dropping SILENTLY is not. Saturation happens when a sink is wedged, which is exactly the
        # incident someone will later read these logs to understand, so the gap has to be visible.
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._worker = threading.Thread(
            target=self._drain,
            name="rutherford-log-writer",
            daemon=True,
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Enqueue one record, evicting the oldest when a blocked sink has exhausted the bounded queue."""
        try:
            message = self.format(record)
        except Exception:
            # * Diagnostics must never block or recursively log a formatting failure.
            return
        try:
            self._records.put_nowait(message)
        except queue.Full:
            # * Preserve the latest lifecycle state, especially terminal finish/error records, under saturation.
            with contextlib.suppress(queue.Empty):
                self._records.get_nowait()
                with self._dropped_lock:
                    self._dropped += 1
            with contextlib.suppress(queue.Full):
                self._records.put_nowait(message)

    def _drain(self) -> None:
        """Drain queued records until closed; a blocked stream can stall only this daemon thread."""
        while True:
            if self._stop_requested.is_set() and self._records.empty():
                return
            try:
                item = self._records.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is _STOP:
                self._report_drops()
                return
            try:
                # * Announce the gap BEFORE the record that follows it, so the loss is anchored in time
                # rather than reported at shutdown when nobody is reading.
                self._report_drops()
                self._stream.write(f"{item}\n")
                self._stream.flush()
            except Exception:
                return

    def _report_drops(self) -> None:
        """Emit one synthetic record for everything discarded since the last report, if anything was.

        Written as JSON like every other record. This stream is one JSON object per line by contract, and
        a plain-text line here would make a shipper treat saturation -- the very incident these logs exist
        to explain -- as malformed input and discard it.
        """
        with self._dropped_lock:
            dropped = self._dropped
        if not dropped:
            return
        payload = json.dumps(
            {"ts": round(time.time(), 3), "event": "log_records_dropped", "count": dropped},
            separators=(",", ":"),
        )
        try:
            self._stream.write(f"{payload}\n")
            self._stream.flush()
        except Exception:
            return  # * Keep the count: an unwritten notice must not clear the debt it was reporting.
        with self._dropped_lock:
            self._dropped -= dropped  # * Subtract, not reset: drops racing this write are still owed.

    def close(self) -> None:
        """Drain a healthy sink briefly, then leave any blocked write isolated on the daemon thread."""
        first_close = not self._stop_requested.is_set()
        if first_close:
            self._stop_requested.set()
            with contextlib.suppress(queue.Full):
                self._records.put_nowait(_STOP)
            self._worker.join(timeout=_LOG_CLOSE_TIMEOUT_S)
        super().close()


def configure_logging(level: str = "info", fmt: str = "json", *, stream: Any | None = None) -> None:
    """Configure structured logging, using a non-blocking background writer for the default stderr sink.

    Idempotent: existing handlers on the logger are closed and cleared first. ``stream`` is injectable for
    deterministic tests and uses a regular synchronous handler; the production ``sys.stderr`` path is queued
    so an MCP host that does not drain its stderr pipe cannot freeze Rutherford's asyncio event loop.
    """
    for old_handler in list(_logger.handlers):
        _logger.removeHandler(old_handler)
        old_handler.close()
    _logger.propagate = False
    if fmt == "off":
        _logger.addHandler(logging.NullHandler())
        _logger.setLevel(logging.CRITICAL + 1)
        return
    handler: logging.Handler = _BackgroundStreamHandler(sys.stderr) if stream is None else logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(_LEVELS.get(level, logging.INFO))


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured JSON log line for ``event`` with the given fields (``None`` fields dropped).

    A no-op when the logger is not enabled for ``level`` (e.g. unconfigured in tests, or
    ``log_format='off'``), so callers can log freely on the hot path without a guard.
    """
    if not _logger.isEnabledFor(level):
        return
    payload: dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
    payload.update({key: value for key, value in fields.items() if value is not None})
    _logger.log(level, json.dumps(payload, default=str, separators=(",", ":")))
