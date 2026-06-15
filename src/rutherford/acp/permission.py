# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The permission policy: how a :class:`~rutherford.domain.enums.SafetyMode` answers an agent's ACP
requests.

Rutherford is the permission authority at the moment of each tool call. ``read_only`` denies filesystem
writes and terminal execution and rejects tool-call permission requests; ``write`` and ``yolo`` allow them.
``propose`` is the in-between: with no sandbox it denies writes (like ``read_only``), but inside a disposable
SANDBOX (the worktree / temp copy a mutating delegation runs in) it ALLOWS writes -- the agent edits the
throwaway worktree so Rutherford can compute the proposed diff, and nothing is ever applied back. So the
policy's write gate depends both on the mode and on whether the session is sandboxed. The OS-level
containment that backs this -- worktree isolation, the FileGateway path guard, the TerminalBroker -- lives in
:mod:`rutherford.acp.sandbox` and :mod:`rutherford.acp.client`.
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
    """A safety mode rendered as ACP permission/filesystem/terminal decisions.

    ``sandboxed`` records whether this session runs inside the disposable worktree / temp copy of a mutating
    delegation. It only matters for ``propose``: a sandboxed propose run allows writes (the agent edits the
    throwaway tree so the diff can be captured; nothing is applied), an un-sandboxed propose run denies them
    (the legacy read-only-equivalent posture). ``read_only`` always denies and ``write`` / ``yolo`` always
    allow, regardless of the flag.
    """

    mode: SafetyMode
    sandboxed: bool = False

    @property
    def allow_fs_read(self) -> bool:
        """Reads are always served (even in read_only); the answer needs to see the code."""
        return True

    @property
    def allow_writes(self) -> bool:
        """Filesystem writes and terminal execution: the mutating modes, plus a SANDBOXED ``propose`` run.

        ``write`` / ``yolo`` always allow. ``propose`` allows only inside a sandbox (it edits the disposable
        worktree, whose diff is captured and discarded); without a sandbox a propose run denies writes like
        ``read_only``. ``read_only`` never allows.
        """
        if self.mode in (SafetyMode.WRITE, SafetyMode.YOLO):
            return True
        return self.mode is SafetyMode.PROPOSE and self.sandboxed

    @property
    def allow_tool_calls(self) -> bool:
        """Whether a tool-call permission request is granted (the same gate as writes for now)."""
        return self.allow_writes

    @property
    def allow_terminal(self) -> bool:
        """Whether the agent may run terminal commands: only the truly mutating modes ``write`` / ``yolo``.

        Deliberately NARROWER than :attr:`allow_writes`: a sandboxed ``propose`` run allows ``fs/write`` (so it
        can edit the throwaway worktree and produce a diff) but must NOT run commands -- propose is "show me
        what you would change", not "build and test it". ``read_only`` denies terminal too.
        """
        return self.mode in (SafetyMode.WRITE, SafetyMode.YOLO)

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
