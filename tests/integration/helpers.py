# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared helpers for the integration suite: opt-in detection and graceful skips."""

from __future__ import annotations

import os

import pytest

from rutherford.context import AppContext
from rutherford.domain.enums import AuthState

#: The opt-in environment variable for each adapter. A CLI runs in the integration suite only
#: when its variable is truthy AND the CLI is installed and authenticated.
CLI_ENV: dict[str, str] = {
    "claude_code": "RUTHERFORD_IT_CLAUDE",
    "codex": "RUTHERFORD_IT_CODEX",
    "antigravity": "RUTHERFORD_IT_ANTIGRAVITY",
    "kiro": "RUTHERFORD_IT_KIRO",
    "opencode": "RUTHERFORD_IT_OPENCODE",
    "goose": "RUTHERFORD_IT_GOOSE",
    "cursor": "RUTHERFORD_IT_CURSOR",
    "qwen": "RUTHERFORD_IT_QWEN",
}

_TRUTHY = {"1", "true", "yes", "on"}


def _opted_in(cli_id: str) -> bool:
    return os.environ.get(CLI_ENV[cli_id], "").strip().lower() in _TRUTHY


def availability_reason(app: AppContext, cli_id: str) -> str | None:
    """Return a skip reason if ``cli_id`` cannot run an integration test, else ``None``."""
    if not _opted_in(cli_id):
        return f"{cli_id} integration disabled; set {CLI_ENV[cli_id]}=1 to enable"
    adapter = app.registry.get(cli_id)
    detected = adapter.detect()
    if not detected.installed:
        return f"{cli_id} binary not installed"
    auth = adapter.check_auth()
    if auth.state in (AuthState.NEEDS_LOGIN, AuthState.API_KEY_MISSING):
        return f"{cli_id} not authenticated: {auth.detail}"
    return None


def is_available(app: AppContext, cli_id: str) -> bool:
    """Return whether ``cli_id`` is opted-in, installed, and not known-unauthenticated."""
    return availability_reason(app, cli_id) is None


def skip_unless_available(app: AppContext, cli_id: str) -> None:
    """Skip the current test (with a clear reason) unless ``cli_id`` is ready."""
    reason = availability_reason(app, cli_id)
    if reason is not None:
        pytest.skip(reason)


def available_clis(app: AppContext) -> list[str]:
    """Return the list of CLI ids ready for integration testing."""
    return [cli_id for cli_id in CLI_ENV if is_available(app, cli_id)]
