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

import json
import logging
import sys
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


def configure_logging(level: str = "info", fmt: str = "json", *, stream: Any | None = None) -> None:
    """Configure the package logger to emit JSON lines to stderr (or silence it when ``fmt='off'``).

    Idempotent: existing handlers on the logger are cleared first, so calling it again re-configures
    cleanly. ``stream`` is injectable for tests; it defaults to ``sys.stderr``.
    """
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
    _logger.propagate = False
    if fmt == "off":
        _logger.addHandler(logging.NullHandler())
        _logger.setLevel(logging.CRITICAL + 1)
        return
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
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
