# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Integration tests: drive a real ACP agent through the unsandboxed write path (local only, -m integration).

Every other test of ``direct_workspace_mutation`` uses the fake ACP agent, which is the right tool for
asserting that the gate's conditions are evaluated in the right order. It cannot answer the question that
actually matters for this capability: does an agent, spawned for real, write into the operator's real tree
with no sandbox between them, and do the refusals refuse before an agent is ever spawned?

Those two are worth a real agent because they are the whole point of the feature and the whole risk of it.
A gate that returns the right error object while an agent is already running in the directory would pass
every unit test here and still be wrong.

The agent reads its credentials from the real home, which the hermetic-home autouse fixture in
``tests/conftest.py`` redirects at a tmp dir -- so these tests restore the real ``USERPROFILE`` / ``HOME``
for themselves (captured at import, before that fixture runs) via ``_real_agent_home``, following the same
pattern as ``tests/integration/test_grok.py``. Only the tests that actually spawn an agent request it; the
refusal tests never reach a spawn and keep the hermetic home.

Slow (real model calls) and deselected by default, like the sibling integration modules.
"""

from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rutherford.acp.roster import build_registry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.io.serialize import decode
from rutherford.runtime.logging import configure_logging
from rutherford.tools.delegate import delegate_tool

pytestmark = pytest.mark.integration

#: The agent driven here. Any installed ACP agent works; this one is the most commonly present.
AGENT = os.environ.get("RUTHERFORD_IT_AGENT", "claude_code")

#: Captured at import (before the hermetic-home autouse fixture runs) so the agent can find its credentials.
_REAL_HOME = {key: os.environ[key] for key in ("USERPROFILE", "HOME") if key in os.environ}


@pytest.fixture
def _real_agent_home(_isolate_config_scopes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the real home for the agent subprocess, which reads its credentials from there.

    Depends on ``_isolate_config_scopes`` so it runs AFTER that autouse fixture has pointed the home at a tmp
    dir, then resets it. Requested only by the tests that spawn an agent, so no other test's isolation moves.
    Rutherford's own role and panel scopes are unaffected: this reaches the agent subprocess, not the
    ``AppContext`` those are loaded into.
    """
    for key, value in _REAL_HOME.items():
        monkeypatch.setenv(key, value)


def _reported_cwd(body: str) -> Path | None:
    """The absolute directory the agent said it was working in, from the file it wrote."""
    for line in reversed([x.strip() for x in body.splitlines() if x.strip()]):
        candidate = Path(line.strip("`\"'"))
        if candidate.is_absolute():
            try:
                return candidate.resolve()
            except OSError:
                return None
    return None


@pytest.fixture
def trusted_root(tmp_path: Path) -> Path:
    """A trusted root for this test only, so the suite never depends on the machine's real allowlist."""
    root = tmp_path / "trusted"
    root.mkdir()
    return root


def _app(trusted_root: Path, *, enabled: bool) -> AppContext:
    config = RutherfordConfig(
        allow_direct_workspace_mutation=enabled,
        trusted_workspaces=[str(trusted_root)],
        default_timeout_s=300.0,
    )
    return build_app_context(config=config, descriptors=build_registry(config))


@pytest.fixture
def audit_stream() -> Iterator[io.StringIO]:
    """Capture the structured log so the admission record can be read back rather than assumed."""
    stream = io.StringIO()
    configure_logging("error", "json", stream=stream)
    yield stream


async def test_a_real_agent_writes_into_the_real_tree_with_no_sandbox(
    trusted_root: Path, audit_stream: io.StringIO, _real_agent_home: None
) -> None:
    """The capability's entire purpose, verified end to end against a spawned agent.

    The file landing in ``working_dir`` is necessary but not sufficient: a sandboxed run produces it in a
    worktree and copies it back, leaving the filesystem looking identical afterwards. Asserting the absent
    diff is not sufficient either -- an implementation that ran sandboxed and merely suppressed those fields
    whenever the flag was set would satisfy it.

    So the agent is asked to record its OWN working directory, which is the one thing the two paths cannot
    share. Under the sandbox it is the worktree; here it must be the operator's real directory. That is a
    positive demonstration that no sandbox stood between the agent and the tree, rather than an inference
    from something being missing.
    """
    work = trusted_root / "project"
    work.mkdir()

    raw = await delegate_tool(
        _app(trusted_root, enabled=True),
        cli=AGENT,
        prompt=(
            "Create a file named exactly hot-test.txt in the current directory. Its contents must be exactly "
            "two lines: the first line RUTHERFORD-HOT-TEST-OK, and the second line the absolute path of the "
            "directory you are currently working in. Then stop."
        ),
        working_dir=str(work),
        safety_mode="write",
        direct_workspace_mutation=True,
        timeout_s=300,
    )
    result = decode(raw).get("result", decode(raw))
    assert result.get("ok") is True, f"the delegation failed: {result.get('error')}"

    written = work / "hot-test.txt"
    assert written.exists(), f"nothing was written into the real tree at {work}"
    body = written.read_text(encoding="utf-8", errors="replace")
    assert "RUTHERFORD-HOT-TEST-OK" in body

    reported = _reported_cwd(body)
    assert reported is not None, f"the agent did not report a usable working directory; wrote {body!r}"
    assert reported == work.resolve(), (
        f"the agent ran in {reported}, not the requested {work.resolve()} -- a sandbox stood in between"
    )

    assert result.get("direct_mutation") is True
    assert result.get("diff") in (None, ""), "a direct run must not report a diff; nothing captured one"
    assert result.get("changed_files") in (None, [], "")

    events = [json.loads(line) for line in audit_stream.getvalue().splitlines() if line.strip()]
    admitted = [e for e in events if e.get("event") == "direct_workspace_mutation_admitted"]
    assert admitted, f"an unsandboxed run against a real agent went unrecorded; saw {[e.get('event') for e in events]}"
    assert admitted[0]["working_dir"] == str(work.resolve()), "the record must name the directory actually written to"


@pytest.mark.parametrize(
    ("label", "enabled", "working_dir_kind", "expected"),
    [
        ("outside the allowlist", True, "outside", "WORKSPACE_NOT_TRUSTED"),
        ("a relative working_dir", True, "relative", "INVALID_INPUT"),
        ("the capability disabled", False, "inside", "INVALID_INPUT"),
    ],
)
async def test_a_refusal_happens_before_an_agent_is_spawned(
    trusted_root: Path, tmp_path: Path, label: str, enabled: bool, working_dir_kind: str, expected: str
) -> None:
    """A refused request must never reach a spawn, which only a real roster can demonstrate.

    With the fake agent a spawn is nearly free, so "refused" and "refused after starting an agent in the
    directory" look the same. Here the agents are real and take seconds to start, so the elapsed time
    separates them: a gate that returned the right error only after launching would fail this.
    """
    work = trusted_root / "project"
    work.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    target = {"outside": str(outside), "relative": ".", "inside": str(work)}[working_dir_kind]

    started = time.monotonic()
    raw = await delegate_tool(
        _app(trusted_root, enabled=enabled),
        cli=AGENT,
        prompt="Create a file named SHOULD-NOT-EXIST.txt containing anything at all.",
        working_dir=target,
        safety_mode="write",
        direct_workspace_mutation=True,
        timeout_s=300,
    )
    elapsed = time.monotonic() - started
    result = decode(raw).get("result", decode(raw))

    assert result.get("ok") is False, f"{label} was admitted"
    assert result["error"]["code"] == expected
    assert elapsed < 2.0, f"{label} took {elapsed:.2f}s, long enough to have spawned an agent first"
    assert not list(outside.glob("SHOULD-NOT-EXIST.txt")), "a refused run still wrote a file"


async def test_a_nested_server_is_refused_with_the_environment_it_would_inherit(
    trusted_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nesting refusal only ever occurs across a process boundary, so drive it through the environment."""
    work = trusted_root / "project"
    work.mkdir(exist_ok=True)
    monkeypatch.setenv("RUTHERFORD_DEPTH", "1")

    raw = await delegate_tool(
        _app(trusted_root, enabled=True),
        cli=AGENT,
        prompt="Create a file named SHOULD-NOT-EXIST.txt.",
        working_dir=str(work),
        safety_mode="write",
        direct_workspace_mutation=True,
        timeout_s=300,
    )
    result = decode(raw).get("result", decode(raw))
    assert result.get("ok") is False and result["error"]["code"] == "INVALID_INPUT"
    assert not (work / "SHOULD-NOT-EXIST.txt").exists()


async def test_the_sandboxed_path_still_captures_what_changed(trusted_root: Path, _real_agent_home: None) -> None:
    """The control. Without the opt-out the same request goes through the sandbox and reports its changes.

    Without this, every assertion above would still pass if direct mutation had silently become the only
    path -- the feature would look correct precisely because nothing else worked.
    """
    work = trusted_root / "sandboxed"
    work.mkdir()

    raw = await delegate_tool(
        _app(trusted_root, enabled=True),
        cli=AGENT,
        prompt=(
            "Create a file named sandboxed.txt whose contents are exactly two lines: the first line "
            "SANDBOXED-OK, and the second line the absolute path of the directory you are currently working "
            "in. Then stop."
        ),
        working_dir=str(work),
        safety_mode="write",
        timeout_s=300,
    )
    result = decode(raw).get("result", decode(raw))
    assert result.get("ok") is True, f"the sandboxed delegation failed: {result.get('error')}"
    assert result.get("direct_mutation") is not True
    assert result.get("changed_files") or result.get("diff"), "the sandboxed path captured nothing that changed"

    # The other half of the proof above: under the sandbox the agent runs somewhere else entirely, so the
    # directory it reports must NOT be the operator's tree. If both paths reported the same directory, the
    # sibling test's cwd assertion would prove nothing.
    produced = work / "sandboxed.txt"
    assert produced.exists(), "the sandboxed run did not apply its file back"
    reported = _reported_cwd(produced.read_text(encoding="utf-8", errors="replace"))
    assert reported is not None, "the agent did not report a usable working directory"
    assert reported != work.resolve(), (
        f"the sandboxed agent ran directly in {work.resolve()}; the isolation did not happen"
    )
