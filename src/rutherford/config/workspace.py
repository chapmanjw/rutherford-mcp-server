# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Read-only judgements about a workspace path.

Separate from :mod:`rutherford.config.trust` on purpose. That module can EDIT the write/yolo allowlist,
so nothing under ``tools/`` -- every one of which is model-callable -- may import it, and a test enforces
that as a total ban. This module only inspects a path and returns text, so a tool can use it freely
without creating a route to the mutating helpers.
"""

from __future__ import annotations

from pathlib import Path


def breadth_warning(workspace: Path | str) -> str | None:
    """A caution when ``workspace`` grants far more than a project directory, or ``None``.

    The write/yolo gate matches by PREFIX, so trusting a directory trusts everything beneath it.
    Trusting a filesystem root, a drive root, a home directory, or the parent of all home directories
    makes essentially the whole machine eligible, which is almost never what someone means by "trust
    this repo".

    Advisory on purpose. The CLI is an explicit human act, and ``setup(trust_workspace=true)`` records a
    working directory the caller already chose; refusing outright would break a legitimate if unusual
    layout such as a checkout at ``/opt`` or ``C:\\repo``. Say it plainly and let the operator decide.
    """
    try:
        resolved = Path(workspace).expanduser().resolve()
    except OSError:
        return None
    if resolved.parent == resolved:
        return f"{resolved} is a filesystem root: this trusts the entire drive"
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        if resolved == home:
            return f"{resolved} is your home directory: this trusts every project and dotfile under it"
        if resolved == home.parent:
            return f"{resolved} contains every user's home directory: this trusts all of them"
    # * A path one level below a root (/home, /Users, C:\\Users, /opt) is a container of many trees rather
    # than one project. len(parts) counts the anchor, so 2 means exactly one segment deep.
    if len(resolved.parts) <= 2:
        return f"{resolved} is a top-level directory: this trusts everything beneath it"
    return None
