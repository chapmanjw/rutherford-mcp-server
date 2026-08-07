# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the agent-facing documentation contract: AGENTS.md and the CLAUDE.md import that reaches it.

``AGENTS.md`` holds the guidance every coding agent is expected to follow. Claude Code reads only the name
``CLAUDE.md``, so that file exists solely to import this one. The arrangement has a sharp edge worth a test:
an import whose target is missing fails **silently**. There is no error and no warning -- the session simply
runs with no project context, and the first sign of trouble is an agent doing something the conventions
forbid. A rename, a move, or a case slip would cost the whole contract with nothing to notice it by.

So the import is checked here rather than trusted. It is a cheap test guarding an expensive, invisible
failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: An ``@path`` import at the start of a line. Claude Code does not treat an ``@`` inside a code span or a
#: fenced block as an import, so those are stripped before this runs.
_IMPORT = re.compile(r"^@(\S+)\s*$", re.MULTILINE)


def _strip_code(markdown: str) -> str:
    """Remove fenced blocks and inline code spans, which are documented NOT to be import sites."""
    without_fences = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", without_fences)


def test_claude_md_imports_agents_md() -> None:
    """The redirect must actually be there, and must name the file exactly.

    Case matters and cannot be caught locally on Windows: ``@agents.md`` resolves on a case-insensitive
    filesystem and fails on Linux and macOS, so a developer on Windows can commit a break they never see.
    Asserting the exact spelling is the only place that mismatch gets caught before CI on another OS.
    """
    claude_md = REPO_ROOT / "CLAUDE.md"
    assert claude_md.is_file(), "CLAUDE.md is what Claude Code looks for; without it there is no entry point"

    imports = _IMPORT.findall(_strip_code(claude_md.read_text(encoding="utf-8")))
    assert "AGENTS.md" in imports, (
        f"CLAUDE.md must import AGENTS.md, spelled exactly; found imports: {imports or 'none'}"
    )


@pytest.mark.parametrize("doc", ["CLAUDE.md", "AGENTS.md"])
def test_every_import_target_exists(doc: str) -> None:
    """Every ``@path`` import resolves to a real file, in any agent doc that uses them.

    This is the assertion the whole module exists for. Claude Code resolves a relative import against the
    file containing it, not the working directory, so the check follows the same rule.
    """
    source = REPO_ROOT / doc
    if not source.is_file():
        pytest.skip(f"{doc} is not present")

    for target in _IMPORT.findall(_strip_code(source.read_text(encoding="utf-8"))):
        resolved = (source.parent / target).resolve()
        assert resolved.is_file(), (
            f"{doc} imports {target!r}, which does not exist. A missing import target is silent: the agent "
            f"session would run with no project guidance and nothing would say so."
        )


def test_agents_md_carries_the_guidance_rather_than_the_stub() -> None:
    """Guard against the two files being swapped back, leaving the canonical one empty.

    The failure this prevents is quiet rather than loud: everything still loads, the import still resolves,
    and the guidance is simply gone. Checking that AGENTS.md is substantially longer than the stub that
    points at it is a coarse measure, but it fails when the content moves out from under it.
    """
    agents_md = REPO_ROOT / "AGENTS.md"
    assert agents_md.is_file(), "AGENTS.md is the canonical contract; CLAUDE.md only points at it"

    body = agents_md.read_text(encoding="utf-8")
    assert len(body.splitlines()) > 40, "AGENTS.md looks like a stub; the guidance belongs here, not in CLAUDE.md"
    for expected in ("## Commands", "## Architecture"):
        assert expected in body, f"AGENTS.md lost its {expected!r} section"
