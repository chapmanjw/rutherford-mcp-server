# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The permission policy: how a :class:`~rutherford.domain.enums.SafetyMode` answers an agent's ACP
requests.

Rutherford is the permission authority at the moment of each tool call. ``read_only`` and ``propose`` deny
filesystem writes and terminal execution and reject tool-call permission requests; ``write`` and ``yolo``
allow them. This governs only what the agent routes through ACP -- a real OS sandbox (worktree isolation)
is a later layer, so ``verify``-style enforcement still belongs above this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from acp.schema import PermissionOption

from ..domain.enums import SafetyMode

#: Permission-option suffixes, in preference order within an allow/reject choice (prefer the one-shot form).
_SUFFIX_PREFERENCE = ("_once", "_always")


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """A safety mode rendered as ACP permission/filesystem/terminal decisions."""

    mode: SafetyMode

    @property
    def allow_fs_read(self) -> bool:
        """Reads are always served (even in read_only); the answer needs to see the code."""
        return True

    @property
    def allow_writes(self) -> bool:
        """Filesystem writes and terminal execution: only the mutating modes."""
        return self.mode in (SafetyMode.WRITE, SafetyMode.YOLO)

    @property
    def allow_tool_calls(self) -> bool:
        """Whether a tool-call permission request is granted (the same gate as writes for now)."""
        return self.allow_writes

    def select_permission(self, options: Sequence[PermissionOption]) -> str | None:
        """Choose which permission option to select, or ``None`` to cancel (reject the whole request).

        For a mutating mode, select an ``allow_*`` option; otherwise select a ``reject_*`` option so the
        tool call is declined rather than the turn cancelled. The one-shot form is preferred over the
        persistent ``_always`` form. ``None`` (cancel) is the fallback when the agent offered no option of
        the desired polarity.
        """
        prefix = "allow" if self.allow_tool_calls else "reject"
        for suffix in _SUFFIX_PREFERENCE:
            wanted = f"{prefix}{suffix}"
            for option in options:
                if option.kind == wanted:
                    return option.option_id
        return None
