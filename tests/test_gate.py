# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests that the local gate and the CI gate cannot drift apart.

``scripts/gate.py`` is the single local definition of the gate: ``just check`` calls it, so the stage list
there is what actually runs. CI deliberately keeps its own separately named steps, because a red run showing
"Type check (mypy, strict)" is worth more than one opaque "gate" box when you are trying to find out what
broke.

That leaves two lists, which is one more than can be kept honest by attention alone. They had already
diverged before this test existed: CI ran ``uv build`` and ``just check`` did not, so a local gate could pass
on a commit that CI would reject. The failure mode is quiet and expensive -- you find out on push, after you
believed you were green.

So the lists are compared mechanically here. This is a cheap test standing in for a habit nobody reliably
has.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from importlib.metadata import version as metadata_version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gate as gate_module  # noqa: E402 - the path insert above has to happen first
from gate import STAGES  # noqa: E402 - same

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Setup rather than gating: it installs the environment the stages run in, and has no local counterpart
#: because running the gate at all presupposes it.
_SETUP_COMMANDS = frozenset({"uv sync --locked"})

#: Deliberately CI-only, and deliberately NOT a gate stage. The release-resolve job exists precisely to
#: bypass the lock that every gate stage installs from, so it cannot be one of them: it builds the wheel
#: and resolves its dependencies fresh from the index, which needs network and takes minutes. There IS a
#: local counterpart -- `just server-boot-release` -- kept out of `just check` for the same reasons.
#: Excluded by command rather than by job so a NEW step added to the gate job still has to match.
_CI_ONLY_COMMANDS = frozenset({"uv run --no-project python scripts/check_server_boot.py --wheel"})


def _ci_gate_commands() -> list[str]:
    """Every ``run:`` command in the CI workflow that is a gate stage rather than setup."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    commands = [line.strip() for line in re.findall(r"^\s*run:\s*(.+)$", text, re.MULTILINE)]
    return [c for c in commands if c not in _SETUP_COMMANDS and c not in _CI_ONLY_COMMANDS]


def test_the_local_gate_runs_exactly_what_ci_runs() -> None:
    """The two gate definitions must hold the same commands, in the same order.

    Order is asserted as well as membership, because the stages are not independent: the per-file coverage
    floor reads what the test run produced. A list that agreed on contents but not sequence would pass a
    set comparison and still be wrong.
    """
    local = [command for _name, command in STAGES]
    remote = _ci_gate_commands()

    assert local == remote, (
        "the local gate and CI have drifted.\n"
        f"  scripts/gate.py: {local}\n"
        f"  ci.yml:          {remote}\n"
        "Update both, or move the stage out of the gate entirely -- but do not let them disagree, because a "
        "local pass would then mean nothing about whether CI will pass."
    )


def test_every_stage_has_a_distinct_name() -> None:
    """Stage names key the report, so a duplicate would silently overwrite a result."""
    names = [name for name, _command in STAGES]
    assert len(names) == len(set(names)), f"duplicate stage name in STAGES: {names}"


@pytest.mark.parametrize(("name", "command"), STAGES)
def test_stage_commands_are_argv_safe(name: str, command: str) -> None:
    """Commands are split on whitespace and run without a shell, so they must not need shell semantics.

    A stage containing a pipe, a redirect, or a quoted argument with a space would be silently mangled by
    ``command.split()`` rather than failing loudly. Catching that here is better than discovering it when a
    gate stage quietly runs the wrong thing.
    """
    assert not any(ch in command for ch in "|><&$"), f"stage {name!r} needs shell semantics: {command!r}"
    assert '"' not in command and "'" not in command, f"stage {name!r} has quoting split() would break: {command!r}"


def test_the_coverage_floor_runs_after_the_tests_that_feed_it() -> None:
    """A real ordering constraint, asserted rather than left as a comment.

    ``check_per_file_coverage.py`` reads the coverage data written by the test run. Reordering these would
    not fail loudly; it would report the previous run's coverage, or none at all.
    """
    names = [name for name, _command in STAGES]
    assert names.index("test") < names.index("coverage-per-file")


def test_the_report_encodes_as_toon() -> None:
    """The normal path uses the project's own serialization seam rather than a second encoder."""
    text, encoding = gate_module._encode({"schema": 1, "verdict": "pass", "stages": [{"name": "lint", "ok": True}]})
    assert encoding == "toon"
    assert "verdict: pass" in text


def test_a_broken_package_downgrades_the_report_instead_of_losing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encoding through the package under test is a coupling; this is the mitigation, tested.

    The seam lives inside the package this gate exists to check, so the one run where importing it fails is
    a run where something is badly wrong -- exactly when silently producing no verdict would be least
    helpful. The format degrades to JSON and the payload says so, rather than the result disappearing.
    """

    class _Blocker:
        def find_spec(self, name: str, target: object = None, path: object = None) -> None:
            if name.startswith("rutherford"):
                raise ImportError("simulated broken package")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])
    for module in [key for key in sys.modules if key.startswith("rutherford")]:
        monkeypatch.delitem(sys.modules, module)

    payload: dict[str, object] = {"schema": 1, "verdict": "pass"}
    text, encoding = gate_module._encode(payload)

    assert encoding == "json", "a failed import must still yield a verdict"
    assert payload["format_fallback"], "the downgrade must be visible in the payload, not silent"
    assert json.loads(text)["verdict"] == "pass"


def test_server_boot_check_expects_every_registered_tool() -> None:
    """The boot check's tool list must match what the server actually registers.

    The check asserts that a fixed set of tools appears over the wire. If a tool is added and the list is
    not, the check keeps passing while covering less than it claims -- and if one is REMOVED, the check is
    the thing that should fail. Comparing against the live registration keeps the list honest without
    starting a server here.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_server_boot import EXPECTED_TOOLS

    from rutherford.server import mcp

    registered: set[str] = {str(getattr(tool, "name", tool)) for tool in asyncio.run(mcp.list_tools())}
    assert registered == EXPECTED_TOOLS, (
        "scripts/check_server_boot.py EXPECTED_TOOLS has drifted from the server.\n"
        f"  only in the check : {sorted(EXPECTED_TOOLS - registered)}\n"
        f"  only in the server: {sorted(registered - EXPECTED_TOOLS)}"
    )


def test_the_version_helper_reads_this_distribution_not_the_framework() -> None:
    """The helper behind ``serverInfo.version`` resolves to THIS package, not FastMCP.

    Scope, stated honestly: this covers the helper and nothing else. It cannot see whether the value
    actually reaches ``serverInfo.version`` on the wire, which is where the bug lived -- FastMCP fills
    that field with its own version when the argument is omitted, and a client connecting to 3.2.0 was
    shown "3.3.1". A test asserting only this would pass while the field stayed wrong, so the wire
    assertion belongs to ``scripts/check_server_boot.py`` and runs as the ``server-boot`` gate stage.
    Named for what it checks so the two are not confused for one another.
    """
    from importlib.metadata import version

    from rutherford.server import _package_version

    assert _package_version() == version("rutherford-mcp-server")
    assert _package_version() != metadata_version("fastmcp")
