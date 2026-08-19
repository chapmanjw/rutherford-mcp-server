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

#: The handler this module owns on the ROOT logger, tracked by identity so a reconfigure retires exactly
#: the one we installed and never a handler some other component put there.
_root_handler: logging.Handler | None = None


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


class _StreamFormatter(logging.Formatter):
    """Render every record as exactly one JSON object, whoever emitted it.

    :func:`log_event` hands this formatter a message that is already a serialized JSON object, so a record
    on exactly :data:`LOGGER_NAME` with no exception attached passes through untouched -- that is the
    stream's normal case and its shape must not change. The pass-through is keyed on the exact name and
    nothing wider on purpose: ``log_event`` is the only emitter there, so an exact match is the same thing as
    "already serialized", and waving through anything else would put a bare unescaped message on a stream
    that is one JSON object per line by contract.

    Everything else is wrapped in an envelope. Two populations arrive here, and the envelope's event name is
    the only place a reader can tell them apart, so it must not lie about either. Records from this package's
    own sub-loggers -- ``rutherford.server``, ``rutherford.services.consensus``, ``rutherford.acp.sandbox``
    and every other ``getLogger(__name__)`` in the tree -- are first-party diagnostics that simply did not go
    through ``log_event``, and are labelled ``log``. Records from anywhere else are labelled ``foreign_log``,
    which is a real diagnostic claim: it tells whoever is reading that the incident happened outside this
    project, and mislabelling a sub-logger sends them to search a dependency for a failure that is ours. The
    dot in the prefix test is load-bearing -- ``rutherford.`` is this package's namespace, whereas a
    third-party logger merely beginning with the same letters is not ours to adopt.

    The ACP SDK is the reason the foreign case exists at all: from 0.12 it reports handler failures with
    ``logging.exception`` -- where 0.11 respectively converted the error to a JSON-RPC internal error and
    suppressed it outright. Those records carry a multi-line traceback and a ``levelname:name:message``
    header, and emitting them raw would make a log shipper treat an SDK failure -- and every surrounding
    lifecycle record in the same batch -- as malformed input and discard it (see
    :meth:`_BackgroundStreamHandler._report_drops`, which keeps the same contract for its own notice).
    Wrapping them here holds the line, and ``json.dumps`` escapes the traceback's newlines so the whole
    thing stays on one physical line.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Pass a ``log_event`` payload through verbatim; wrap any other record in a JSON envelope."""
        first_party = record.name == LOGGER_NAME or record.name.startswith(f"{LOGGER_NAME}.")
        if record.name == LOGGER_NAME and record.exc_info is None:
            return record.getMessage()
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "event": "log" if first_party else "foreign_log",
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "info", fmt: str = "json", *, stream: Any | None = None) -> None:
    """Configure structured logging, using a non-blocking background writer for the default stderr sink.

    Idempotent: handlers this module previously installed -- on the package logger AND on root -- are removed
    and closed first. ``stream`` is injectable for deterministic tests and uses a regular synchronous handler;
    the production ``sys.stderr`` path is queued so an MCP host that does not drain its stderr pipe cannot
    freeze Rutherford's asyncio event loop.

    The same handler is installed on the ROOT logger, which is not tidiness but the whole point. The ACP SDK
    defines no logger of its own -- there is not one ``getLogger`` call in the package -- and reports handler
    failures with module-level ``logging.exception``, so its records land on root. Module-level
    ``logging.error`` runs ``basicConfig()`` whenever root has no handlers, which installs a plain synchronous
    ``StreamHandler(sys.stderr)`` at NOTSET and leaves it there for the life of the process. From that moment
    every SDK traceback -- and every WARNING from every other library -- is written synchronously from the
    event loop thread, which is precisely the stall :class:`_BackgroundStreamHandler` exists to prevent. Root
    having a handler of ours pre-empts that ``basicConfig`` call entirely.

    Two deliberate restraints. Root's LEVEL is left alone at its WARNING default: raising Rutherford's own
    verbosity must not drag every dependency's debug traffic onto the wire. And foreign root handlers are
    left in place rather than evicted -- they are not ours to remove, and in the shipped stdio entrypoint
    there are none, because Rutherford owns its process.

    ``_logger.propagate`` stays ``False`` for a second reason now: the package logger and root share one
    handler INSTANCE, so propagation would push each Rutherford record through the same handler twice.
    """
    global _root_handler
    retired: list[logging.Handler] = list(_logger.handlers)
    for old_handler in retired:
        _logger.removeHandler(old_handler)
    if _root_handler is not None:
        logging.root.removeHandler(_root_handler)
        # * Dedupe by identity, not equality: the package logger and root normally share one handler, and
        # closing a _BackgroundStreamHandler twice would join its writer thread twice for no reason.
        if not any(old_handler is _root_handler for old_handler in retired):
            retired.append(_root_handler)
        _root_handler = None
    for old_handler in retired:
        old_handler.close()

    _logger.propagate = False
    if fmt == "off":
        _logger.addHandler(logging.NullHandler())
        _logger.setLevel(logging.CRITICAL + 1)
        # * Silenced still needs a root handler. "off" means Rutherford emits nothing, not that the SDK is
        # free to install a synchronous stderr writer behind our back -- and with no handler here that is
        # exactly what its first logging.exception would do.
        _root_handler = logging.NullHandler()
        logging.root.addHandler(_root_handler)
        return
    handler: logging.Handler = _BackgroundStreamHandler(sys.stderr) if stream is None else logging.StreamHandler(stream)
    handler.setFormatter(_StreamFormatter())
    _logger.addHandler(handler)
    _logger.setLevel(_LEVELS.get(level, logging.INFO))
    logging.root.addHandler(handler)
    _root_handler = handler


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
