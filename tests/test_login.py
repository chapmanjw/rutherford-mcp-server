# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for ACP sign-in: recognizing "Authentication required", ``authenticate``, and the ``login`` tool.

Every case drives the real fake ACP agent over stdio with its sign-in simulation on
(``RUTHERFORD_FAKE_AUTH_MARKER``), so the AUTH_REQUIRED classification, the authenticate call, the stderr
link scan, and the background-job hand-off all run end to end with no browser.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest
from acp import RequestError
from acp.schema import AuthMethodAgent, InitializeResponse

from rutherford import server
from rutherford.acp.auth import (
    AuthMethodInfo,
    auth_methods_of,
    describe_methods,
    extract_urls,
    is_auth_required,
)
from rutherford.acp.conformance import probe_agent, probe_connection
from rutherford.acp.descriptors import AgentDescriptor, DescriptorRegistry
from rutherford.acp.failures import indicates_unhealthy
from rutherford.acp.permission import PermissionPolicy
from rutherford.acp.session import ACPHandshakeError, ACPSession
from rutherford.config.schema import RutherfordConfig
from rutherford.context import AppContext, build_app_context
from rutherford.domain.enums import JobStatus, SafetyMode
from rutherford.domain.error_codes import ErrorCode
from rutherford.domain.errors import RutherfordError
from rutherford.io.serialize import decode
from rutherford.tools.delegate import delegate_tool
from rutherford.tools.jobs import cancel_job_tool
from rutherford.tools.login import login_tool
from tests.conftest import DrainAsyncJob

_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
_SIGNIN_URL = "https://example.test/oauth?state=abc&code_challenge=xyz"


def _descriptor(marker: Path, **env: str) -> AgentDescriptor:
    overrides = (("RUTHERFORD_FAKE_AUTH_MARKER", str(marker)), *env.items())
    return AgentDescriptor("fake", "Fake", _FAKE_CMD, env_overrides=tuple(overrides))


def _app(descriptor: AgentDescriptor) -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([descriptor]))


def _session(descriptor: AgentDescriptor, cwd: Path, **kwargs: Any) -> ACPSession:
    return ACPSession(descriptor, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(cwd), **kwargs)


# --- pure helpers ------------------------------------------------------------------------------------------


def test_auth_methods_of_reads_every_kind_and_salvaged_dicts() -> None:
    init = InitializeResponse(protocol_version=1, auth_methods=[AuthMethodAgent(id="oauth", name="OAuth")])
    assert auth_methods_of(init) == [AuthMethodInfo(id="oauth", name="OAuth")]

    class Salvaged:  # ACP 0.12 can hand back raw dicts where it could not parse an entry
        auth_methods: ClassVar[list[object]] = [
            {"id": "key", "name": "Key", "type": "env_var", "vars": [{"name": "K", "secret": False}], "link": 7},
            {"id": "term", "type": "terminal", "args": ["--login", 3], "env": {"A": 1}},
            {"id": "", "name": "no id"},
            {"name": "missing id"},
            "not a method",
        ]

    methods = auth_methods_of(Salvaged())
    assert [m.id for m in methods] == ["key", "term"]
    key, term = methods
    assert key.kind == "env_var" and key.env_vars[0].name == "K" and key.env_vars[0].secret is False
    assert key.link is None  # a non-string link is dropped, not stringified
    assert term.kind == "terminal" and term.name == "term" and term.terminal_args == ("--login", "3")
    assert term.terminal_env == {"A": "1"}
    assert auth_methods_of(object()) == []

    class NotAList:
        auth_methods = "oauth"

    assert auth_methods_of(NotAList()) == []


def test_method_payload_only_carries_fields_its_kind_uses() -> None:
    agent = AuthMethodInfo(id="o", name="O", description="browser").as_payload()
    assert agent == {"id": "o", "name": "O", "kind": "agent", "description": "browser"}
    env = AuthMethodInfo(id="k", name="K", kind="env_var", link="https://x.test").as_payload()
    assert env["env_vars"] == [] and env["link"] == "https://x.test"
    assert AuthMethodInfo(id="t", name="T", kind="terminal", terminal_args=("--l",)).as_payload()["terminal_args"] == [
        "--l"
    ]


def test_is_auth_required_matches_only_the_reserved_code() -> None:
    assert is_auth_required(RequestError.auth_required())
    assert not is_auth_required(RequestError.internal_error())
    assert not is_auth_required(RuntimeError("Authentication required"))  # the code decides, not the text


def test_describe_methods() -> None:
    assert describe_methods([]) == "it advertises no sign-in methods"
    assert describe_methods([AuthMethodInfo(id="a", name="A")]) == "it advertises: a (A)"


def test_extract_urls_is_https_only_deduped_trimmed_and_capped() -> None:
    text = (
        "Open (https://a.test/x?state=1). then http://plain.test and javascript:alert(1) "
        "https://a.test/x?state=1 https://b.test https://c.test https://d.test https://"
    )
    assert extract_urls(text) == ["https://a.test/x?state=1", "https://b.test", "https://c.test"]
    assert extract_urls("no links here") == []


def test_auth_required_benches_a_seat() -> None:
    assert indicates_unhealthy(ErrorCode.AUTH_REQUIRED)


# --- the session --------------------------------------------------------------------------------------------


async def test_session_reports_auth_required_with_methods_and_next_step(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker"), tmp_path)
    with pytest.raises(ACPHandshakeError) as caught:
        await session.open()
    assert caught.value.code is ErrorCode.AUTH_REQUIRED
    assert "fake-oauth (Log in with Fake)" in caught.value.message
    assert "login agent=fake" in caught.value.message
    assert [m.id for m in session.auth_methods] == ["fake-oauth", "fake-key", "fake-term"]


async def test_session_signs_in_then_opens(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    session = _session(_descriptor(marker), tmp_path, auth_method="fake-oauth")
    await session.open()
    try:
        assert session.session_id == "fake-session-1"
        assert marker.exists()
        assert session.signin_urls == [_SIGNIN_URL]
    finally:
        await session.close()
    # The credential persists: a later ordinary session opens with no authenticate call.
    plain = _session(_descriptor(marker), tmp_path)
    await plain.open()
    await plain.close()
    assert plain.signin_urls == []  # the link scan only runs on a sign-in session


async def test_signin_link_is_found_past_the_stderr_capture_cap(tmp_path: Path) -> None:
    # 20 KiB of log before the link: far past the 8 KiB head kept for failure details.
    session = _session(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_NOISE="20000"), tmp_path, auth_method="fake-oauth"
    )
    await session.open()
    await session.close()
    assert session.signin_urls == [_SIGNIN_URL]


async def test_session_refuses_an_unadvertised_or_non_agent_method(tmp_path: Path) -> None:
    for method, phrase in (("nope", "does not offer"), ("fake-key", "env_var method")):
        session = _session(_descriptor(tmp_path / "marker"), tmp_path, auth_method=method)
        with pytest.raises(ACPHandshakeError) as caught:
            await session.open()
        assert caught.value.code is ErrorCode.INVALID_INPUT and phrase in caught.value.message


async def test_failed_signin_is_auth_failed(tmp_path: Path) -> None:
    session = _session(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_FAIL="1"), tmp_path, auth_method="fake-oauth"
    )
    with pytest.raises(ACPHandshakeError) as caught:
        await session.open()
    assert caught.value.code is ErrorCode.AUTH_FAILED and "Onboarding failed" in caught.value.message


async def test_signin_that_outlives_its_budget_is_auth_failed(tmp_path: Path) -> None:
    session = _session(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="5"),
        tmp_path,
        auth_method="fake-oauth",
        auth_timeout_s=0.5,
    )
    with pytest.raises(ACPHandshakeError) as caught:
        await session.open()
    assert caught.value.code is ErrorCode.AUTH_FAILED and "within 0.5s" in caught.value.message


# --- doctor and delegate ------------------------------------------------------------------------------------


async def test_doctor_reports_auth_required_with_a_hint(tmp_path: Path) -> None:
    descriptor = _descriptor(tmp_path / "marker")
    connect = await probe_connection(descriptor, timeout_s=30.0)
    assert connect.status == "auth_required" and connect.installed and not connect.connected
    assert connect.remediation_hint is not None and "login agent=fake" in connect.remediation_hint
    full = await probe_agent(descriptor, timeout_s=30.0)
    assert full.status == "auth_required" and full.installed and not full.answered
    assert full.remediation_hint is not None and "login agent=fake" in full.remediation_hint


async def test_delegate_to_an_unsigned_agent_fails_auth_required(tmp_path: Path) -> None:
    app = _app(_descriptor(tmp_path / "marker"))
    data = decode(await delegate_tool(app, cli="fake", prompt="hi", working_dir=str(tmp_path)))
    assert data["ok"] is False and data["error"]["code"] == "AUTH_REQUIRED"


# --- the login tool -----------------------------------------------------------------------------------------


async def test_login_without_method_lists_methods_and_changes_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    data = decode(await login_tool(_app(_descriptor(marker)), agent="fake"))
    assert data["status"] == "method_required"
    assert [m["kind"] for m in data["methods"]] == ["agent", "env_var", "terminal"]
    assert "login agent=fake method=" in data["next"]
    assert not marker.exists()


async def test_login_pending_returns_the_link_then_the_job_signs_in(
    tmp_path: Path, drain_async_job: DrainAsyncJob
) -> None:
    marker = tmp_path / "marker"
    app = _app(_descriptor(marker, RUTHERFORD_FAKE_AUTH_DELAY="1.5"))
    data = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    assert data["status"] == "pending" and data["signin_urls"] == [_SIGNIN_URL]
    assert "did not open them" in data["note"]
    record = await drain_async_job(app.jobs, data["job_id"])
    assert record.error is None and record.result is not None
    assert decode(record.result)["status"] == "authenticated"
    assert marker.exists()
    again = decode(await login_tool(app, agent="fake", method="fake-oauth"))
    assert again["status"] == "authenticated"  # signed in already: no second consent screen


async def test_cancelling_a_pending_login_stops_the_signin(tmp_path: Path, drain_async_job: DrainAsyncJob) -> None:
    marker = tmp_path / "marker"
    app = _app(_descriptor(marker, RUTHERFORD_FAKE_AUTH_DELAY="30"))
    data = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    assert data["status"] == "pending" and data["signin_urls"] == [_SIGNIN_URL]
    cancelled = decode(await cancel_job_tool(app, job_id=data["job_id"]))
    assert cancelled["status"] == "cancelled"
    record = await drain_async_job(app.jobs, data["job_id"])
    assert record.status is JobStatus.CANCELLED and record.result is None
    assert not marker.exists()


async def test_login_that_finishes_inside_the_wait_returns_the_outcome(tmp_path: Path) -> None:
    # An agent that signs in with no link to open (an existing credential it can reuse) finishes in the wait.
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_SILENT="1"))
    data = decode(await login_tool(app, agent="fake", method="fake-oauth"))
    assert data["status"] == "authenticated" and data["method"] == "fake-oauth"


async def test_login_with_no_wait_reports_pending_without_a_link(
    tmp_path: Path, drain_async_job: DrainAsyncJob
) -> None:
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="1"))
    data = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=0))
    assert data["status"] == "pending" and "printed no sign-in link" in data["note"]
    await drain_async_job(app.jobs, data["job_id"])


async def test_login_failure_inside_the_wait_raises_auth_failed(tmp_path: Path) -> None:
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_FAIL="1", RUTHERFORD_FAKE_AUTH_SILENT="1"))
    with pytest.raises(RutherfordError) as caught:
        await login_tool(app, agent="fake", method="fake-oauth")
    assert caught.value.code is ErrorCode.AUTH_FAILED


async def test_login_env_var_and_terminal_methods_say_what_to_do(tmp_path: Path) -> None:
    app = _app(_descriptor(tmp_path / "marker"))
    env = decode(await login_tool(app, agent="fake", method="fake-key"))
    assert env["status"] == "configure_env" and "FAKE_API_KEY" in env["next"]
    assert "[agents.fake.env]" in env["next"] and env["link"] == "https://example.test/keys"
    term = decode(await login_tool(app, agent="fake", method="fake-term"))
    assert term["status"] == "run_in_terminal" and term["command"] == [*_FAKE_CMD, "--login"]


async def test_login_rejects_bad_input(tmp_path: Path) -> None:
    app = _app(_descriptor(tmp_path / "marker"))
    with pytest.raises(RutherfordError) as caught:
        await login_tool(app, agent="fake", method="nope")
    assert caught.value.code is ErrorCode.INVALID_INPUT and "fake-oauth" in caught.value.message
    for kwargs in ({"wait_s": -1.0}, {"wait_s": 121.0}, {"timeout_s": 0.0}, {"timeout_s": 1801.0}):
        with pytest.raises(RutherfordError) as bad:
            await login_tool(app, agent="fake", method="fake-oauth", **kwargs)
        assert bad.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(RutherfordError) as unknown:
        await login_tool(app, agent="ghost")
    assert unknown.value.code is ErrorCode.UNKNOWN_TARGET


async def test_login_on_an_agent_needing_no_signin_or_not_installed(tmp_path: Path) -> None:
    plain = AgentDescriptor("fake", "Fake", _FAKE_CMD)
    assert decode(await login_tool(_app(plain), agent="fake"))["status"] == "authenticated"
    missing = AgentDescriptor("fake", "Fake", ("this-binary-does-not-exist-xyz123",))
    assert decode(await login_tool(_app(missing), agent="fake", method="x"))["status"] == "not_installed"
    dead = AgentDescriptor("fake", "Fake", (sys.executable, "-c", "import sys; sys.exit(0)"))
    assert decode(await login_tool(_app(dead), agent="fake"))["status"] == "handshake_failed"


async def test_login_mcp_tool_wraps_the_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_APP", _app(_descriptor(tmp_path / "marker")))
    assert "method_required" in await server.login(agent="fake")
