# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration: live Cursor launch ``--model`` routing vs session store.db (opt-in, -m integration).

Proves the only working Cursor contract on cursor-agent 2026.06+: the effective model rides the process
launch argv (``cursor-agent acp --model <id>``), not in-session ACP ``set_config_option`` /
``set_model``. Exact opt-in ids are ``composer-2.5[fast=false]`` and ``grok-4.5[effort=high,fast=false]``.
A unique marker ties Rutherford's ``session_id`` to ``~/.cursor/acp-sessions/<id>/store.db``; the blob
graph is searched for ``providerOptions.cursor.modelName`` matching the expected runtime family
via explicit prefixes (Grok: ``grok-`` / ``cursor-grok-``; Composer: ``composer-``), not a bare
substring. A ``*-fast`` runtime slug is not treated as failure: live Cursor/entitlement
often forces fast even when launch requested ``fast=false``.

Skips when ``cursor-agent`` is missing, the turn fails, or the session DB is absent -- no user-specific
paths (resolves under ``Path.home()`` after restoring the real home). Deselected by default with the rest
of the integration suite; do not run as part of ordinary unit CI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from rutherford.acp.descriptors import default_registry
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import run_acp_turn
from rutherford.domain.enums import SafetyMode
from rutherford.domain.models import DelegationResult

pytestmark = pytest.mark.integration

#: Captured at import (before the hermetic-home autouse fixture) so Cursor can find auth + write acp-sessions.
_REAL_HOME = {key: os.environ[key] for key in ("USERPROFILE", "HOME") if key in os.environ}
_CURSOR_INSTALLED = shutil.which("cursor-agent") is not None
#: Exact user opt-in Grok bracket id (launch ``--model``); family routing must land a Grok runtime.
_GROK_MODEL = "grok-4.5[effort=high,fast=false]"
#: Exact user opt-in Composer bracket id (launch ``--model``); family routing must land a Composer runtime.
_COMPOSER_MODEL = "composer-2.5[fast=false]"
#: Live turn budget; Cursor ACP handshake + short read-only reply should finish well under this.
_LIVE_TURN_TIMEOUT_S = 90.0
#: Allowed ``providerOptions.cursor.modelName`` prefixes per launch family (case-insensitive startswith).
#: Explicit prefixes avoid false positives from unrelated ids that merely contain a family substring.
_RUNTIME_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    "grok": ("grok-", "cursor-grok-"),
    "composer": ("composer-",),
}


@pytest.fixture
def _real_agent_home(_isolate_config_scopes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real home so Cursor finds credentials and writes under ``~/.cursor/acp-sessions``."""
    for key, value in _REAL_HOME.items():
        monkeypatch.setenv(key, value)


def _acp_sessions_root() -> Path:
    """Cursor's ACP session store under the real home (not hermetic tmp)."""
    return Path.home() / ".cursor" / "acp-sessions"


def _model_names_from_store(db_path: Path) -> list[str]:
    """Collect ``providerOptions.cursor.modelName`` strings from a closed session ``store.db`` (read-only)."""
    names: list[str] = []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("select data from blobs").fetchall()
    for (data,) in rows:
        if not data:
            continue
        try:
            text = bytes(data).decode("utf-8", "replace")
        except Exception:
            continue
        if "modelName" not in text and "providerOptions" not in text:
            continue
        # * Prefer structured JSON walks; fall back to a regex when a blob is a fragment.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            names.extend(re.findall(r'"modelName"\s*:\s*"([^"]+)"', text))
            continue
        names.extend(_walk_model_names(payload))
    return names


def _walk_model_names(node: object) -> list[str]:
    """Depth-first collect cursor modelName values from nested JSON."""
    found: list[str] = []
    if isinstance(node, dict):
        cursor = node.get("cursor")
        if isinstance(cursor, dict):
            name = cursor.get("modelName")
            if isinstance(name, str):
                found.append(name)
        provider = node.get("providerOptions")
        if isinstance(provider, dict):
            found.extend(_walk_model_names(provider))
        for value in node.values():
            found.extend(_walk_model_names(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_model_names(item))
    return found


def _runtime_matches_family(names: list[str], family: str) -> bool:
    """True when any runtime name starts with an allowed prefix for ``family``.

    Matching is case-insensitive ``startswith`` against :data:`_RUNTIME_FAMILY_PREFIXES` (not a bare
    substring). Unknown families never match. Runtime speed (``*-fast``) is not inspected.
    """
    prefixes = _RUNTIME_FAMILY_PREFIXES.get(family.lower())
    if not prefixes:
        return False
    for name in names:
        lowered = name.lower()
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return True
    return False


async def _assert_launch_model_in_session_db(*, model: str, runtime_family: str) -> DelegationResult:
    """Run a read-only launch-``--model`` turn and assert store.db records ``runtime_family``.

    Envelope stays honest: ``selected_model`` / ``provenance.confirmed`` are not claimed from argv
    (ACP has no runtime attestation). Family match uses explicit runtime prefixes (see
    :func:`_runtime_matches_family`). A ``*-fast`` runtime slug is allowed: Cursor may force fast
    despite an exact ``fast=false`` launch id.
    """
    sessions_root = _acp_sessions_root()
    if not sessions_root.is_dir():
        pytest.skip("Cursor acp-sessions directory is missing under the real home")

    marker = f"RUTHERFORD-CURSOR-MODEL-{uuid.uuid4().hex[:12]}"
    cursor = default_registry().get("cursor")
    assert cursor.model_launch_flag == "--model"
    prompt = (
        f"Reply with ONLY the token {marker} and nothing else. Do not call tools. This is a read-only identity check."
    )
    result = await run_acp_turn(
        cursor,
        prompt,
        policy=PermissionPolicy(SafetyMode.READ_ONLY),
        cwd=str(Path.cwd()),
        timeout_s=_LIVE_TURN_TIMEOUT_S,
        model=model,
    )
    assert result.ok is True, f"cursor turn failed: {result.error}"
    assert result.session_id is not None
    assert result.argv is not None
    assert result.argv[-2:] == ["--model", model]
    assert result.requested_model == model
    assert result.target.model == model
    assert result.selected_model is None
    assert result.provenance is not None and result.provenance.confirmed is False

    store = sessions_root / result.session_id / "store.db"
    if not store.is_file():
        pytest.skip(f"Cursor session store missing for session_id={result.session_id}")

    model_names = _model_names_from_store(store)
    assert model_names, f"no providerOptions.cursor.modelName in {store}"
    assert _runtime_matches_family(model_names, runtime_family), (
        f"expected a {runtime_family!r} runtime modelName after launch --model {model!r}, "
        f"got {model_names!r}; marker={marker} session_id={result.session_id}"
    )
    # * Evidence for the live-validation report (opt-in suite; not ordinary CI).
    print(
        f"cursor_model_routing marker={marker} session_id={result.session_id} "
        f"requested={model!r} runtime_modelNames={model_names!r}"
    )
    return result


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_routes_grok_in_session_db(_real_agent_home: None) -> None:
    """Read-only turn with launch ``--model`` Grok fast=false; session store must record a Grok family runtime."""
    await _assert_launch_model_in_session_db(model=_GROK_MODEL, runtime_family="grok")


@pytest.mark.skipif(not _CURSOR_INSTALLED, reason="cursor-agent is not installed")
async def test_cursor_launch_model_routes_composer_in_session_db(_real_agent_home: None) -> None:
    """Launch ``--model`` Composer fast=false; session store must record a Composer family runtime."""
    await _assert_launch_model_in_session_db(model=_COMPOSER_MODEL, runtime_family="composer")
