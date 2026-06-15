# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for continue_job (item 9): resume vs re-injection, the continuation link, and the fresh trust gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.domain.models import DelegationRequest, RunRecord, Target
from rutherford.io.ledger import read_record
from rutherford.tools.continue_job import _reinjection_prompt, continue_job_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
FAKE = AgentDescriptor("fake", "Fake", _FAKE_CMD)
# A fake that does NOT advertise the ACP loadSession capability, so a resume against it is RESUME_FAILED.
NO_RESUME = AgentDescriptor(
    "fake_noresume", "No Resume", _FAKE_CMD, env_overrides=(("RUTHERFORD_FAKE_NO_LOADSESSION", "1"),)
)


def _app(tmp_path: Path) -> AppContext:
    config = RutherfordConfig(jobs_dir=str(tmp_path / "jobs"))
    return build_app_context(config=config, descriptors=DescriptorRegistry([FAKE, NO_RESUME]))


def _children(app: AppContext, parent_run_id: str) -> list[RunRecord]:
    """Every persisted record that continues ``parent_run_id`` (the continuation's child records)."""
    out: list[RunRecord] = []
    for run_dir in app.ledger.root.iterdir():
        if run_dir.is_dir() and (run_dir / "state.json").exists():
            record = read_record(run_dir)
            if record.continued_from == parent_run_id:
                out.append(record)
    return out


def _write_parent(app: AppContext, **overrides: object) -> RunRecord:
    """Persist a hand-built delegate parent record so a continuation has something to read."""
    fields: dict[str, object] = {
        "run_id": "parent01",
        "kind": "delegate",
        "cli": "fake",
        "session_id": "fake-session-1",
        "prompt": "what is 17 + 25?",
        "cwd": str(REPO_ROOT),
    }
    fields.update(overrides)
    record = RunRecord(**fields)  # type: ignore[arg-type]
    app.ledger.write(record, answer="42")
    return record


# --- resume vs re-injection --------------------------------------------------


async def test_continue_resumes_a_real_persisted_delegate(tmp_path: Path) -> None:
    # A real persisted delegate records its session id; continue_job resumes that exact session (WHOAMI proves
    # the fake reloaded it), and the continuation is a child record linked to the parent.
    app = _app(tmp_path)
    parent = await app.delegation.delegate(
        DelegationRequest(
            target=Target(cli="fake"), prompt="what is 17 + 25?", persist=True, working_dir=str(REPO_ROOT)
        )
    )
    assert parent.run_dir is not None
    parent_id = Path(parent.run_dir).name

    out = await continue_job_tool(app, job_id=parent_id, prompt="WHOAMI", working_dir=str(REPO_ROOT))
    assert "resumed the agent session" in out  # the notice surfaces the resume path (9-E)
    assert "resumed=yes" in out  # the fake confirms it loaded the prior session

    children = _children(app, parent_id)
    assert len(children) == 1 and children[0].kind == "delegate" and children[0].ok
    assert children[0].continued_from == parent_id  # the forward link (9-B), parent untouched


async def test_continue_reinjects_when_the_agent_cannot_resume(tmp_path: Path) -> None:
    # The parent recorded a session, but its agent does not advertise loadSession: the resume attempt fails
    # RESUME_FAILED and continue_job falls back to re-injecting the prior context (9-E).
    app = _app(tmp_path)
    _write_parent(app, cli="fake_noresume")
    out = await continue_job_tool(app, job_id="parent01", prompt="and double it", working_dir=str(REPO_ROOT))
    assert "does not support resume" in out and "re-injected" in out  # surfaced fallback
    children = _children(app, "parent01")
    # EXACTLY one child: the failed-resume probe's stray record is dropped, so the chain keeps only the real
    # re-injected continuation (9-B). A double-persist would leave a second, RESUME_FAILED record here.
    assert len(children) == 1 and children[0].ok


async def test_continue_reinjects_when_no_session_was_recorded(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _write_parent(app, session_id=None)  # parent minted no resumable session
    out = await continue_job_tool(app, job_id="parent01", prompt="WHOAMI", working_dir=str(REPO_ROOT))
    assert "no resumable session was recorded" in out


def test_reinjection_prompt_carries_prompt_and_answer_in_order() -> None:
    prompt = _reinjection_prompt("What is 17 + 25?", "42", "Now double it.")
    assert "What is 17 + 25?" in prompt and "42" in prompt and "Now double it." in prompt
    assert prompt.index("What is 17 + 25?") < prompt.index("42") < prompt.index("Now double it.")
    # an empty prior answer is omitted, not rendered as a blank "answer" section
    no_answer = _reinjection_prompt("Q", "", "next")
    assert "prior answer" not in no_answer and "Q" in no_answer and "next" in no_answer


# --- fresh trust gate (9-D) --------------------------------------------------


async def test_continue_does_not_inherit_the_parents_write_mode(tmp_path: Path) -> None:
    # The parent ran in write mode; the continuation must re-gate fresh and default to read_only, not inherit
    # write -- the new direction may change intent (9-D).
    app = _app(tmp_path)
    _write_parent(app, safety_mode=SafetyMode.WRITE)
    await continue_job_tool(app, job_id="parent01", prompt="WHOAMI", working_dir=str(REPO_ROOT))
    children = _children(app, "parent01")
    assert children and all(child.safety_mode is SafetyMode.READ_ONLY for child in children)  # not WRITE


async def test_continue_defaults_read_only_even_in_a_write_default_workspace(tmp_path: Path) -> None:
    # 9-D: the continuation default is read_only -- not even a workspace-wide default_safety_mode=write leaks
    # into an unvetted new direction. Only an EXPLICIT safety_mode (with trust) escalates.
    config = RutherfordConfig(jobs_dir=str(tmp_path / "jobs"), default_safety_mode=SafetyMode.WRITE)
    app = build_app_context(config=config, descriptors=DescriptorRegistry([FAKE, NO_RESUME]))
    app.ledger.write(
        RunRecord(
            run_id="p01", kind="delegate", cli="fake", session_id="fake-session-1", prompt="q", cwd=str(REPO_ROOT)
        ),
        answer="a",
    )
    await continue_job_tool(app, job_id="p01", prompt="WHOAMI", working_dir=str(REPO_ROOT))
    children = _children(app, "p01")
    assert children and all(child.safety_mode is SafetyMode.READ_ONLY for child in children)


# --- guards ------------------------------------------------------------------


async def test_continue_rejects_a_panel_kind(tmp_path: Path) -> None:
    # v1 continues a single delegate (9-A); a consensus / debate parent is refused with a clear message.
    app = _app(tmp_path)
    app.ledger.write(RunRecord(run_id="panel01", kind="consensus", cli="fake", prompt="x"), answer="yes")
    with pytest.raises(RutherfordError) as exc:
        await continue_job_tool(app, job_id="panel01", prompt="more")
    assert exc.value.code is ErrorCode.INVALID_INPUT and "not supported yet" in exc.value.message


async def test_continue_unknown_job_is_not_found(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with pytest.raises(RutherfordError) as exc:
        await continue_job_tool(app, job_id="deadbeef", prompt="x")
    assert exc.value.code is ErrorCode.JOB_NOT_FOUND


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".", ""])
async def test_continue_rejects_a_job_id_that_escapes_the_jobs_root(tmp_path: Path, bad: str) -> None:
    app = _app(tmp_path)
    with pytest.raises(RutherfordError) as exc:
        await continue_job_tool(app, job_id=bad, prompt="x")
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_continue_rejects_a_symlinked_job_dir_escaping_the_root(tmp_path: Path) -> None:
    # Defense in depth: a single-component job id whose entry is a symlink pointing OUTSIDE the jobs root is
    # rejected, so a continuation can only read a record the ledger itself wrote. Skipped where the platform
    # cannot create a symlink (Windows without the privilege).
    app = _app(tmp_path)
    app.ledger.root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    (outside / "artifacts").mkdir(parents=True)
    app.ledger.write(RunRecord(run_id="real", kind="delegate", cli="fake", prompt="x"), answer="y")
    (outside / "state.json").write_text((app.ledger.root / "real" / "state.json").read_text(), encoding="utf-8")
    try:
        (app.ledger.root / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    with pytest.raises(RutherfordError) as exc:
        await continue_job_tool(app, job_id="escape", prompt="x")
    assert exc.value.code is ErrorCode.INVALID_INPUT


async def test_continue_unknown_parent_agent_is_unknown_target(tmp_path: Path) -> None:
    # The parent named an agent no longer in the registry -> a clean UNKNOWN_TARGET, not a crash.
    app = _app(tmp_path)
    app.ledger.write(RunRecord(run_id="gone01", kind="delegate", cli="vanished", prompt="x"), answer="y")
    with pytest.raises(RutherfordError) as exc:
        await continue_job_tool(app, job_id="gone01", prompt="more")
    assert exc.value.code is ErrorCode.UNKNOWN_TARGET


async def test_continue_corrupt_record_is_invalid_input(tmp_path: Path) -> None:
    app = _app(tmp_path)
    run_dir = app.ledger.root / "corrupt01"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(RutherfordError) as exc:
        await continue_job_tool(app, job_id="corrupt01", prompt="x")
    assert exc.value.code is ErrorCode.INVALID_INPUT and "corrupt" in exc.value.message


async def test_continue_async_returns_a_job_id(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _write_parent(app)
    out = await continue_job_tool(app, job_id="parent01", prompt="WHOAMI", working_dir=str(REPO_ROOT), mode="async")
    assert "job_id" in out
