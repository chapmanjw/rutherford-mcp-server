# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for ACP sign-in: recognizing "Authentication required", ``authenticate``, and the ``login`` tool.

Every case drives the real fake ACP agent over stdio with its sign-in simulation on
(``RUTHERFORD_FAKE_AUTH_MARKER``), so the AUTH_REQUIRED classification, the authenticate call, the stderr
link scan, and the background-job hand-off all run end to end with no browser.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

import psutil
import pytest
from acp import RequestError
from acp.schema import AuthMethodAgent, InitializeResponse
from fastmcp.exceptions import ToolError

from rutherford import server
from rutherford.acp.auth import (
    AuthMethodInfo,
    auth_methods_of,
    carries_credential,
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
from rutherford.tools import login as login_module
from rutherford.tools.delegate import delegate_tool
from rutherford.tools.jobs import cancel_job_tool
from rutherford.tools.login import login_tool
from tests.conftest import DrainAsyncJob

_FAKE_CMD = (sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py"))
_SIGNIN_URL = "https://example.test/oauth?state=abc&code_challenge=xyz"
#: A JWT-shaped value for the masking tests: three dot-joined segments, assembled so no token literal is committed.
_FAKE_JWT = ".".join(["eyJ" + "hbGciOiJub25lIn0", "eyJ" + "zdWIiOiJmYWtlIn0", "ZmFrZXNpZ25hdHVyZQ"])


def _descriptor(marker: Path, **env: str) -> AgentDescriptor:
    overrides = (("RUTHERFORD_FAKE_AUTH_MARKER", str(marker)), *env.items())
    return AgentDescriptor("fake", "Fake", _FAKE_CMD, env_overrides=tuple(overrides))


def _app(descriptor: AgentDescriptor) -> AppContext:
    return build_app_context(config=RutherfordConfig(), descriptors=DescriptorRegistry([descriptor]))


def _pids(pid_file: Path) -> list[int]:
    """The PIDs of every fake agent launched so far, oldest first (one line per launch)."""
    if not pid_file.exists():
        return []
    return [int(line) for line in pid_file.read_text(encoding="utf-8").split()]


def _entry(job_id: str, descriptor: AgentDescriptor, cwd: Path, *, torn_down: asyncio.Event, ran: bool) -> Any:
    """A hand-built in-flight sign-in entry; ``ran`` says whether its job body ever started (spawned an agent)."""
    started = asyncio.Event()
    if ran:
        started.set()
    return login_module._ActiveSignin(
        job_id=job_id,
        method="fake-oauth",
        timeout_s=1.0,
        session=_session(descriptor, cwd),
        torn_down=torn_down,
        started=started,
    )


def _alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _session(descriptor: AgentDescriptor, cwd: Path, **kwargs: Any) -> ACPSession:
    return ACPSession(descriptor, policy=PermissionPolicy(SafetyMode.READ_ONLY), cwd=str(cwd), **kwargs)


# --- pure helpers ------------------------------------------------------------------------------------------


def test_auth_methods_of_reads_every_kind_and_salvaged_dicts() -> None:
    init = InitializeResponse(protocol_version=1, auth_methods=[AuthMethodAgent(id="oauth", name="OAuth")])
    assert auth_methods_of(init) == [AuthMethodInfo(id="oauth", name="OAuth")]

    class Salvaged:  # ACP 0.12 can hand back raw dicts where it could not parse an entry
        auth_methods: ClassVar[list[object]] = [
            {"id": "key", "name": "Key", "type": "env_var", "vars": [{"name": "K", "secret": False}], "link": 7},
            {"id": "term", "type": "terminal", "args": ["--login", "3"], "env": {"A": "1"}},
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


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://keys.test/",
        "https://trusted.test@attacker.test/keys",
        "https://keys.test:99999/",
        "https://[bad/",
        "https://",
        "https://keys.test/a b",
        # A credential page link carrying a credential would put a live secret into the tool result.
        "https://keys.test/cb?access_token=abc123def456",
        "https://keys.test/cb?TOKEN=abc",
        "https://keys.test/file?sig=abc",
        "https://bucket.test/o?X-Amz-Signature=deadbeef&X-Amz-Expires=60",
        "https://keys.test/cb?state=s&client_secret=xyz",
        "https://keys.test/cb#id_token=" + _FAKE_JWT,
    ],
)
def test_env_var_link_must_be_a_safe_https_url(link: str) -> None:
    class Advertised:
        auth_methods: ClassVar[list[object]] = [{"id": "k", "name": "K", "type": "env_var", "vars": [], "link": link}]

    assert auth_methods_of(Advertised())[0].link is None


def test_env_var_link_keeps_a_plain_https_url() -> None:
    class Advertised:
        auth_methods: ClassVar[list[object]] = [
            {"id": "k", "name": "K", "type": "env_var", "vars": [], "link": "https://keys.test/new?x=1"}
        ]

    assert auth_methods_of(Advertised())[0].link == "https://keys.test/new?x=1"


def test_carries_credential_spots_named_parameters_and_shapes() -> None:
    assert carries_credential("https://k.test/?Signature=abc")  # case-insensitive name
    fake_key_id = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled so a secret scanner never sees a key-shaped literal
    assert carries_credential(f"https://k.test/path/{fake_key_id}")  # a shape outside the query
    assert not carries_credential("https://k.test/settings/api-keys?tab=new&state=s")  # mentions keys, holds none
    # Implicit-flow fragments: an opaque token matches no shape, so its parameter name must catch it.
    assert carries_credential("https://login.test/#access_token=opaque-value")
    assert carries_credential("https://login.test/cb#state=s&id_token=opaque")
    assert not carries_credential("https://k.test/docs#getting-a-key")  # an ordinary page anchor


def test_method_payload_only_carries_fields_its_kind_uses() -> None:
    agent = AuthMethodInfo(id="o", name="O", description="browser").as_payload()
    assert agent == {"id": "o", "name": "O", "kind": "agent", "description": "browser"}
    env = AuthMethodInfo(id="k", name="K", kind="env_var", link="https://x.test").as_payload()
    assert env["env_vars"] == [] and env["link"] == "https://x.test"
    assert AuthMethodInfo(id="t", name="T", kind="terminal", terminal_args=("--l",)).as_payload()["terminal_args"] == [
        "--l"
    ]


def test_is_auth_required_needs_the_reserved_code_and_auth_wording() -> None:
    assert is_auth_required(RequestError.auth_required())
    assert not is_auth_required(RequestError.internal_error())
    assert not is_auth_required(RuntimeError("Authentication required"))  # must be an ACP RequestError
    # -32000 is also JSON-RPC's generic server error; without auth wording it is not a sign-in refusal.
    assert not is_auth_required(RequestError(-32000, "Internal backend crashed"))
    assert is_auth_required(RequestError(-32000, "Please log in first"))


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
    # Userinfo makes the displayed host differ from the opened one; a malformed port cannot be parsed.
    spoofed = (
        "Sign in: https://accounts.example.com@attacker.test/x https://[bad https://h.test:99999/ https://ok.test/y"
    )
    assert extract_urls(spoofed) == ["https://ok.test/y"]


def test_extract_urls_only_reads_sign_in_prompt_lines() -> None:
    text = (
        "I0000 connecting to https://endpoint.test/v1?auth=token\n"  # "auth" only inside the URL: not a prompt
        "update available at https://updates.test/latest\n"
        "Open the following link to authenticate: https://login.test/oauth?state=s\n"
    )
    assert extract_urls(text) == ["https://login.test/oauth?state=s"]


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


async def test_decoy_urls_before_the_prompt_are_not_the_signin_link(tmp_path: Path) -> None:
    session = _session(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DECOY="1"), tmp_path, auth_method="fake-oauth"
    )
    await session.open()
    await session.close()
    assert session.signin_urls == [_SIGNIN_URL]


async def test_signin_links_are_capped_across_the_whole_session(tmp_path: Path) -> None:
    session = _session(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_EXTRA_LINKS="5"), tmp_path, auth_method="fake-oauth"
    )
    await session.open()
    await session.close()
    assert session.signin_urls == [_SIGNIN_URL, "https://extra0.test/oauth", "https://extra1.test/oauth"]


def test_advertised_method_strings_are_stripped_of_controls() -> None:
    class Advertised:
        auth_methods: ClassVar[list[object]] = [
            {"id": "ok", "name": "Log\x1b[31m in\u202e", "description": "a\x07b" + "x" * 600},
            {"id": "bad\u202eid", "name": "dropped: its id would change"},
            {"id": "t-ok", "type": "terminal", "args": ["--login", "--quiet"], "env": {"MODE": "cli"}},
            {"id": "t-arg", "type": "terminal", "args": ["--login\x1b[0m"]},
            {"id": "t-nul", "type": "terminal", "args": ["\x00"]},
            {"id": "t-key", "type": "terminal", "env": {"K\u2066": "v"}},
            {"id": "t-empty", "type": "terminal", "env": {"": "v"}},
            {"id": "t-value", "type": "terminal", "env": {"OK": "v\x1b]0;title\x07"}},
            {"id": "t-nonstr", "type": "terminal", "args": ["--login", 3]},
            {"id": "e", "type": "env_var", "vars": [{"name": "KEY\x00"}, {"name": "OK", "label": "L\u202a"}]},
        ]

    ok, term, env = auth_methods_of(Advertised())
    assert ok.name == "Log in" and ok.description is not None
    assert ok.description.startswith("ab") and len(ok.description) == 512
    # A terminal command is shown to run: one altered part drops the whole method, never a rewritten command.
    assert term.id == "t-ok" and term.terminal_args == ("--login", "--quiet") and term.terminal_env == {"MODE": "cli"}
    assert [(v.name, v.label) for v in env.env_vars] == [("OK", "L")]  # a variable name with a control is dropped


async def test_resuming_on_a_signed_out_agent_is_auth_required(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker"), tmp_path, resume_session_id="prior-session")
    with pytest.raises(ACPHandshakeError) as caught:
        await session.open()
    assert caught.value.code is ErrorCode.AUTH_REQUIRED and "login agent=fake" in caught.value.message


async def test_a_generic_32000_stays_a_handshake_failure(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_GENERIC_32000="1"), tmp_path)
    with pytest.raises(ACPHandshakeError) as caught:
        await session.open()
    assert caught.value.code is ErrorCode.ACP_HANDSHAKE_FAILED and "backend crashed" in caught.value.message


async def test_a_prompt_at_the_end_of_a_very_long_line_is_found(tmp_path: Path) -> None:
    # End to end: 40 KiB of one line, then the wording, then (in a later read) the URL. Which read crosses the
    # carry cap depends on pipe timing, so the trim itself is pinned by the deterministic unit test below.
    session = _session(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_LONG_PREFIX="40960"), tmp_path, auth_method="fake-oauth"
    )
    await session.open()
    await session.close()
    assert session.signin_urls == [_SIGNIN_URL]


def test_a_link_split_across_two_reads_is_found_whole(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker"), tmp_path, auth_method="fake-oauth")
    session._scan_chunk_for_urls(b"I0000 noise\nOpen this link to sign in: https://login.test/oa")
    assert session.signin_urls == []  # the partial line is carried, not scanned as a truncated link
    session._scan_chunk_for_urls(b"uth?state=s\n")
    assert session.signin_urls == ["https://login.test/oauth?state=s"]


async def test_a_prompt_with_no_trailing_newline_is_scanned_at_eof(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker"), tmp_path, auth_method="fake-oauth")
    stream = asyncio.StreamReader()
    stream.feed_data(b"Open this link to sign in: https://login.test/oauth?state=s")
    stream.feed_eof()
    await session._drain_stderr(stream)
    assert session.signin_urls == ["https://login.test/oauth?state=s"]


def test_a_link_mangled_by_credential_masking_is_dropped(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker"), tmp_path, auth_method="fake-oauth")
    jwt = _FAKE_JWT
    session._scan_chunk_for_urls(f"Open this link to sign in: https://login.test/oauth?id_token_hint={jwt}\n".encode())
    session._scan_chunk_for_urls(f"Or sign in at https://login.test/o?hint={jwt}&state=s\n".encode())
    assert session.signin_urls == []  # handed over masked, the link would not work


def test_an_over_long_partial_line_keeps_its_end_for_the_link_scan(tmp_path: Path) -> None:
    # One read pushes the partial line past the cap with the prompt wording at its very end; the URL and the
    # newline arrive in the next read. Dropping the over-long carry loses the wording, and with it the link.
    session = _session(_descriptor(tmp_path / "marker"), tmp_path, auth_method="fake-oauth")
    session._scan_chunk_for_urls(b"x" * 20_000 + b" Open the following link to authenticate: ")
    session._scan_chunk_for_urls(b"https://login.test/oauth?state=s\n")
    assert session.signin_urls == ["https://login.test/oauth?state=s"]


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
    marker, pid_file = tmp_path / "marker", tmp_path / "agent.pid"
    app = _app(_descriptor(marker, RUTHERFORD_FAKE_AUTH_DELAY="30", RUTHERFORD_FAKE_PID_FILE=str(pid_file)))
    data = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    assert data["status"] == "pending" and data["signin_urls"] == [_SIGNIN_URL]
    # The signing-in agent is the last one to start (the read-only state check before it has already exited).
    agent = psutil.Process(_pids(pid_file)[-1])
    assert agent.is_running()
    cancelled = decode(await cancel_job_tool(app, job_id=data["job_id"]))
    assert cancelled["status"] == "cancelled"
    record = await drain_async_job(app.jobs, data["job_id"])
    assert record.status is JobStatus.CANCELLED and record.result is None
    await asyncio.to_thread(psutil.wait_procs, [agent], timeout=15.0)
    assert not _alive(agent), "cancelling the sign-in left the agent process running"
    assert not marker.exists()


async def test_a_repeated_login_joins_the_running_signin(tmp_path: Path, drain_async_job: DrainAsyncJob) -> None:
    marker, pid_file = tmp_path / "marker", tmp_path / "agent.pid"
    app = _app(_descriptor(marker, RUTHERFORD_FAKE_AUTH_DELAY="2", RUTHERFORD_FAKE_PID_FILE=str(pid_file)))
    first = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    launched = _pids(pid_file)
    second = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    assert second["status"] == "pending" and second["job_id"] == first["job_id"]
    assert first["joined"] is False and second["joined"] is True
    assert second["signin_urls"] == [_SIGNIN_URL]
    with pytest.raises(RutherfordError) as other:  # a different method may not silently ride the running one
        await login_tool(app, agent="fake", method="fake-key")
    assert other.value.code is ErrorCode.INVALID_INPUT and "already running" in other.value.message
    assert _pids(pid_file) == launched  # joining launched no agent at all, not even a state check
    jobs = [record for record in await app.jobs.list() if record.tool == "login"]
    assert len(jobs) == 1
    await drain_async_job(app.jobs, first["job_id"])
    after = decode(await login_tool(app, agent="fake", method="fake-oauth"))
    assert after["status"] == "authenticated"  # the finished job no longer blocks a fresh state check


async def test_concurrent_logins_start_one_signin(tmp_path: Path, drain_async_job: DrainAsyncJob) -> None:
    pid_file = tmp_path / "agent.pid"
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="2", RUTHERFORD_FAKE_PID_FILE=str(pid_file)))
    results = await asyncio.gather(*(login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0) for _ in range(3)))
    job_ids = {decode(result)["job_id"] for result in results}
    assert len(job_ids) == 1
    # One state check plus one sign-in: unserialized, each of the three calls would launch both.
    assert len(_pids(pid_file)) == 2
    await drain_async_job(app.jobs, job_ids.pop())


async def test_a_login_right_after_cancel_starts_a_fresh_signin(tmp_path: Path, drain_async_job: DrainAsyncJob) -> None:
    # End to end only: the ordering (no new launch before the old agent is torn down) is proven by
    # test_a_finished_signin_blocks_a_new_one_until_its_teardown_completes.
    pid_file = tmp_path / "agent.pid"
    app = _app(
        _descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="30", RUTHERFORD_FAKE_PID_FILE=str(pid_file))
    )
    first = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    old_agent = psutil.Process(_pids(pid_file)[-1])
    await cancel_job_tool(app, job_id=first["job_id"])
    second = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    assert second["status"] == "pending" and second["job_id"] != first["job_id"]
    assert not _alive(old_agent)
    await cancel_job_tool(app, job_id=second["job_id"])
    await drain_async_job(app.jobs, second["job_id"])


async def test_a_finished_signin_blocks_a_new_one_until_its_teardown_completes(
    tmp_path: Path, drain_async_job: DrainAsyncJob
) -> None:
    descriptor = _descriptor(tmp_path / "marker")
    app = _app(descriptor)

    async def _finished(_on_activity: object) -> str:
        return "{}"

    job_id = await app.jobs.submit("login", _finished)
    await drain_async_job(app.jobs, job_id)
    torn_down = asyncio.Event()
    entry = _entry(job_id, descriptor, tmp_path, torn_down=torn_down, ran=True)
    login_module._ACTIVE.setdefault(app.jobs, {})["fake"] = entry
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, torn_down.set)  # the job has ended, but its agent is still being closed
    started = loop.time()
    assert await login_module._active_signin(app, "fake") is None
    assert loop.time() - started >= 0.45  # it waited for the teardown rather than clearing the slot at once
    assert "fake" not in login_module._ACTIVE[app.jobs]


async def test_an_evicted_signin_still_waits_for_its_agent_to_close(tmp_path: Path) -> None:
    # The job record is gone from the store (evicted), but the job ran and its agent is still being closed:
    # the slot must stay held until teardown, or a new login could launch a second agent first.
    descriptor = _descriptor(tmp_path / "marker")
    app = _app(descriptor)
    torn_down = asyncio.Event()
    entry = _entry("evicted-job", descriptor, tmp_path, torn_down=torn_down, ran=True)
    login_module._ACTIVE.setdefault(app.jobs, {})["fake"] = entry
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, torn_down.set)
    started = loop.time()
    assert await login_module._active_signin(app, "fake") is None
    assert loop.time() - started >= 0.45


async def test_an_evicted_signin_holds_off_a_new_login_launch(tmp_path: Path, drain_async_job: DrainAsyncJob) -> None:
    pid_file = tmp_path / "agent.pid"
    descriptor = _descriptor(
        tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="2", RUTHERFORD_FAKE_PID_FILE=str(pid_file)
    )
    app = _app(descriptor)
    torn_down = asyncio.Event()
    login_module._ACTIVE.setdefault(app.jobs, {})["fake"] = _entry(
        "evicted-job", descriptor, tmp_path, torn_down=torn_down, ran=True
    )
    task = asyncio.create_task(login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    await asyncio.sleep(3.0)  # long enough for an unblocked login to have launched its state-check agent
    assert not task.done() and _pids(pid_file) == []  # no agent launched while the old one is still closing
    torn_down.set()
    data = decode(await task)
    assert data["status"] == "pending" and data["joined"] is False
    await drain_async_job(app.jobs, data["job_id"])


async def test_a_signin_cancelled_before_it_ran_frees_the_slot_at_once(tmp_path: Path) -> None:
    descriptor = _descriptor(tmp_path / "marker")
    app = _app(descriptor)
    gate = asyncio.Event()

    async def _never(_on_activity: object) -> str:  # pragma: no cover - cancelled before it is scheduled
        await gate.wait()
        return "{}"

    job_id = await app.jobs.submit("login", _never)
    await app.jobs.cancel(job_id)  # synchronously, before the task has had a chance to start
    record = await app.jobs.get(job_id)
    assert record.started_at is None
    # torn_down is never set: the job never ran, so there is no teardown to wait for.
    entry = _entry(job_id, descriptor, tmp_path, torn_down=asyncio.Event(), ran=False)
    login_module._ACTIVE.setdefault(app.jobs, {})["fake"] = entry
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await login_module._active_signin(app, "fake") is None
    assert loop.time() - started < 1.0


async def test_a_successful_login_lifts_the_seats_bench(tmp_path: Path) -> None:
    app = build_app_context(
        config=RutherfordConfig(cooldown_threshold=1, cooldown_duration_s=600.0),
        descriptors=DescriptorRegistry([_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_SILENT="1")]),
    )
    data = decode(await delegate_tool(app, cli="fake", prompt="hi", working_dir=str(tmp_path)))
    assert data["error"]["code"] == "AUTH_REQUIRED"
    assert app.delegation.is_benched("fake")  # signed out -> benched
    assert decode(await login_tool(app, agent="fake", method="fake-oauth"))["status"] == "authenticated"
    assert not app.delegation.is_benched("fake")


async def test_a_read_only_login_that_finds_the_agent_signed_in_lifts_its_bench(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    app = build_app_context(
        config=RutherfordConfig(cooldown_threshold=1, cooldown_duration_s=600.0),
        descriptors=DescriptorRegistry([_descriptor(marker)]),
    )
    await delegate_tool(app, cli="fake", prompt="hi", working_dir=str(tmp_path))
    assert app.delegation.is_benched("fake")
    marker.write_text("signed-in", encoding="utf-8")  # signed in some other way, e.g. the agent's own CLI
    assert decode(await login_tool(app, agent="fake"))["status"] == "authenticated"
    assert not app.delegation.is_benched("fake")


async def test_when_closed_fires_only_once_the_teardown_task_finishes(tmp_path: Path) -> None:
    session = _session(_descriptor(tmp_path / "marker"), tmp_path)
    calls: list[str] = []
    session.when_closed(lambda: calls.append("never-opened"))  # no teardown was ever started: runs at once
    assert calls == ["never-opened"]

    release = asyncio.Event()

    async def _slow_teardown() -> None:
        await release.wait()

    session._close_task = asyncio.create_task(_slow_teardown())
    session.when_closed(lambda: calls.append("after-teardown"))
    await asyncio.sleep(0.05)
    assert calls == ["never-opened"]  # close() may have returned, but the agent is not gone yet
    release.set()
    await session._close_task
    await asyncio.sleep(0)  # done callbacks run on the next loop iteration
    assert calls == ["never-opened", "after-teardown"]
    session.when_closed(lambda: calls.append("already-done"))  # a finished teardown: runs at once
    assert calls == ["never-opened", "after-teardown", "already-done"]


async def test_the_signin_dir_is_removed_only_after_the_agent_is_gone(tmp_path: Path) -> None:
    cwd, torn_down = tmp_path / "login-dir", asyncio.Event()
    cwd.mkdir()
    session = _session(_descriptor(tmp_path / "marker"), tmp_path)
    release = asyncio.Event()

    async def _slow_teardown() -> None:
        await release.wait()

    session._close_task = asyncio.create_task(_slow_teardown())
    session.when_closed(lambda: login_module._finish_teardown(cwd, torn_down))
    await asyncio.sleep(0.05)
    assert cwd.exists() and not torn_down.is_set()
    release.set()
    await session._close_task
    await asyncio.sleep(0)
    assert not cwd.exists() and torn_down.is_set()


async def test_a_signin_job_evicted_mid_wait_is_a_clean_job_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain_async_job: DrainAsyncJob
) -> None:
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="2", RUTHERFORD_FAKE_AUTH_SILENT="1"))
    real_get = app.jobs.get
    submitted: list[str] = []
    real_submit = app.jobs.submit

    async def _submit(*args: Any, **kwargs: Any) -> str:
        job_id = await real_submit(*args, **kwargs)
        submitted.append(job_id)
        return job_id

    async def _get(job_id: str) -> Any:
        if job_id in submitted:  # the sign-in job vanished from the store while the caller waited for its link
            raise RutherfordError(ErrorCode.JOB_NOT_FOUND, f"no job with id {job_id!r}")
        return await real_get(job_id)

    monkeypatch.setattr(app.jobs, "submit", _submit)
    monkeypatch.setattr(app.jobs, "get", _get)
    with pytest.raises(RutherfordError) as caught:
        await login_tool(app, agent="fake", method="fake-oauth", wait_s=5.0)
    assert caught.value.code is ErrorCode.JOB_NOT_FOUND and "no longer tracked" in caught.value.message
    monkeypatch.setattr(app.jobs, "get", real_get)
    await drain_async_job(app.jobs, submitted[0])


async def test_a_signin_that_does_not_take_is_auth_failed(tmp_path: Path) -> None:
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_NOSTICK="1", RUTHERFORD_FAKE_AUTH_SILENT="1"))
    with pytest.raises(RutherfordError) as caught:
        await login_tool(app, agent="fake", method="fake-oauth")
    assert caught.value.code is ErrorCode.AUTH_FAILED and "still refused a session" in caught.value.message


async def test_a_joining_login_waits_for_the_link(tmp_path: Path, drain_async_job: DrainAsyncJob) -> None:
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_DELAY="2", RUTHERFORD_FAKE_AUTH_NOISE="1"))
    first = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=0))
    assert first["status"] == "pending"
    second = decode(await login_tool(app, agent="fake", method="fake-oauth", wait_s=10.0))
    assert second["joined"] is True and second["signin_urls"] == [_SIGNIN_URL]
    await drain_async_job(app.jobs, first["job_id"])


async def test_a_signin_that_never_starts_creates_no_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    app = _app(_descriptor(tmp_path / "marker"))

    async def _full(*_args: object, **_kwargs: object) -> str:
        raise RutherfordError(ErrorCode.TOO_MANY_JOBS, "the job store is full")

    monkeypatch.setattr(app.jobs, "submit", _full)
    with pytest.raises(RutherfordError) as caught:
        await login_tool(app, agent="fake", method="fake-oauth")
    assert caught.value.code is ErrorCode.TOO_MANY_JOBS
    assert list((tmp_path / "tmp").glob("rutherford-acp-login-*")) == []


async def test_login_leaves_no_temp_dir_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    app = _app(_descriptor(tmp_path / "marker", RUTHERFORD_FAKE_AUTH_SILENT="1"))
    assert decode(await login_tool(app, agent="fake", method="fake-oauth"))["status"] == "authenticated"
    assert list((tmp_path / "tmp").glob("rutherford-acp-login-*")) == []


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
    assert "advertised by the agent" in env["link_note"]
    term = decode(await login_tool(app, agent="fake", method="fake-term"))
    assert term["status"] == "run_in_terminal" and term["command"] == [*_FAKE_CMD, "--login"]
    assert "advertised by the agent" in term["command_note"]


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
    data = decode(await server.login(agent="fake"))
    assert data["status"] == "method_required" and data["agent"] == "fake"
    marker = tmp_path / "marker"
    marker.write_text("signed-in", encoding="utf-8")  # now signed in: the wrapper reports it as such
    assert decode(await server.login(agent="fake", method="fake-oauth"))["status"] == "authenticated"
    with pytest.raises(ToolError):  # a service error surfaces as an MCP tool error, not an envelope
        await server.login(agent="fake", wait_s=-1.0)
