# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ``setup`` tool (first-run helper): show where config lives and scaffold a starter ``config.toml``.

The "good duck" getting-started surface. It resolves the config path for a scope (``global`` or
``project``), generates a sensible commented starter ``config.toml`` at the effective defaults, and -- opt
in -- writes it without ever clobbering an existing file. A small roster snapshot (the count and ids of the
currently-registered agents) lets the caller see what they already have before they write anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config.loader import default_global_config_path
from ..config.schema import RutherfordConfig
from ..context import AppContext, tool_success
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError

#: The two scopes ``setup`` understands, listed in error messages.
SCOPES = ("global", "project")


def _starter_config(config: RutherfordConfig, *, trust_workspace: bool, cwd: Path) -> str:
    """Build the starter ``config.toml`` as a hand-written, commented TOML string.

    The most useful settings appear at their *effective* defaults (read from ``config``) with a one-line
    comment each, plus a commented-out local-model example and a ``trusted_workspaces`` line. When
    ``trust_workspace`` is set the current ``cwd`` is written into ``trusted_workspaces`` (uncommented) so
    write/yolo delegations are permitted there out of the box. Built by hand on purpose -- no toml-writer
    dependency -- and it must round-trip through ``tomllib`` and validate against :class:`RutherfordConfig`.
    """
    safety = config.default_safety_mode.value
    timeout = _format_number(config.default_timeout_s)
    auto_detect = "true" if config.auto_detect_local_models else "false"
    max_targets = config.max_targets
    persistence = config.default_persistence
    synthesize = "true" if config.synthesize_default else "false"
    lines = [
        "# Rutherford config -- a starter scaffold. Every setting below is OPTIONAL and shown at its",
        "# effective default; delete what you don't need or uncomment a line to change it. See the",
        "# project README and docs/ for the full schema.",
        "",
        f'default_safety_mode = "{safety}"   # read_only | propose | write | yolo (write/yolo need a trusted ws)',
        f"default_timeout_s = {timeout}        # per-run wall-clock timeout, in seconds",
        f"auto_detect_local_models = {auto_detect}  # probe a running Ollama (:11434) / LM Studio (:1234) for voices",
        f"max_targets = {max_targets}             # most agents a single consensus call may fan out to",
        "",
        "# Durability (F2): persist runs to disk as replayable jobs. 'ephemeral' (default) keeps nothing",
        "# unless a call passes persist=true; 'job' persists every run unless a call passes persist=false.",
        f'default_persistence = "{persistence}"   # ephemeral | job',
        '# jobs_dir = "/abs/path/to/jobs"   # where durable runs land (default: <cwd>/.rutherford/jobs)',
        f"synthesize_default = {synthesize}        # have consensus write a combined answer across the voices",
        "",
        "# Absolute paths under which write/yolo delegations are permitted. Read_only/propose never need this.",
    ]
    if trust_workspace:
        trusted = _toml_str(str(cwd))
        lines.append(f"trusted_workspaces = [{trusted}]")
    else:
        lines.append('# trusted_workspaces = ["/abs/path/to/a/workspace/you/trust"]')
    lines += [
        "",
        "# A local model as a first-class voice: point a built-in agent at a local runtime. Uncomment and",
        "# adjust (needs a tool-capable model loaded in the backend). See docs/local-models.md.",
        "# [agents.local-goose]",
        '# base = "goose"        # the built-in agent to launch',
        '# backend = "ollama"    # ollama | lmstudio',
        '# model = "qwen3:8b"    # the model id the runtime serves (required)',
        "",
        "# Named multi-agent panels live in a sibling panels.toon (next to this file), not here. Each panel",
        '# names its targets/strategy so a call can say panel = "<name>" instead of listing seats. After',
        "# editing panels.toon, call the reload_panels tool. See docs/ for the panels.toon schema.",
        "",
    ]
    return "\n".join(lines)


def _format_number(value: float) -> str:
    """Render a float without a trailing ``.0`` so the TOML reads as a whole number when it is one."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _toml_str(value: str) -> str:
    """Quote ``value`` as a TOML basic string, escaping backslashes and double quotes (Windows paths)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _target_path(scope: str, cwd: Path) -> Path:
    """Resolve the config path for ``scope``: the global ``config.toml`` or ``<cwd>/.rutherford/config.toml``."""
    if scope == "global":
        return default_global_config_path()
    return cwd / ".rutherford" / "config.toml"


async def setup_tool(
    app: AppContext,
    *,
    scope: str = "project",
    write: bool = False,
    trust_workspace: bool = False,
) -> str:
    """Show where config lives and scaffold a starter ``config.toml``; with ``write`` create it (never clobber).

    ``scope`` is ``project`` (``<cwd>/.rutherford/config.toml``) or ``global`` (the platform config dir's
    ``config.toml``). The starter content sits at the effective defaults; ``trust_workspace`` adds the
    current cwd to ``trusted_workspaces``. With ``write=False`` (default) the proposed ``content`` and
    ``path`` are returned without touching disk; with ``write=True`` the parent dirs are created and the
    file is written -- but an existing file is left untouched and reported (``written=false``).
    """
    if scope not in SCOPES:
        options = ", ".join(SCOPES)
        raise RutherfordError(ErrorCode.INVALID_INPUT, f"unknown scope {scope!r}; choose one of: {options}")

    cwd = Path.cwd()
    path = _target_path(scope, cwd)
    content = _starter_config(app.config, trust_workspace=trust_workspace, cwd=cwd)
    agent_ids = app.descriptors.ids()

    exists = path.exists()
    written = False
    note: str | None = None
    if write:
        if exists:
            # Never clobber: report the existing file rather than overwrite a user's config.
            note = "a config already exists at this path; not overwritten"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            written = True

    result: dict[str, Any] = {
        "scope": scope,
        "path": str(path),
        "exists": exists,
        "written": written,
        "already_exists": write and exists,
        "agent_count": len(agent_ids),
        "agents": agent_ids,
        "content": content,
    }
    if note is not None:
        result["note"] = note
    return tool_success(result)
