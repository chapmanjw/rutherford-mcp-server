# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""A controllable in-process ACP agent for tests, run as ``python -m tests.fake_acp_agent``.

It implements just enough of the ACP ``Agent`` role to drive :func:`rutherford.acp.session.run_acp_turn`
without a real CLI. Behaviour is selected by trigger words in the prompt, so a test can exercise each path:
a normal answer (``17 + 25`` -> ``42``, else an echo), ``REFUSE`` (``stopReason`` refusal), ``EMPTY``
(a clean turn with no answer text), and ``HANG`` (a long sleep, for a timeout).

To drive the consensus strategies a test needs the answer text to carry a chosen verdict. A
``SAY=<text>`` token anywhere in the prompt makes the agent answer with exactly ``<text>`` (everything
after ``SAY=`` up to a newline), so a test can plant a ``VERDICT: yes`` line or a ``{"verdict": "no"}``
JSON object verbatim. Without a ``SAY=`` token the legacy echo behaviour is unchanged.

To drive the time-budget harvest a test needs a voice that runs LONG. A ``SLEEP=<seconds>`` token makes the
agent stream a short partial line and then sleep for ``<seconds>`` before finishing, so a panel deadline
shorter than the sleep cuts the voice in flight (and harvests the streamed partial). A bare ``SLEEP`` (no
``=``) sleeps a long default. The partial is streamed as an ``agent_message_chunk`` BEFORE the sleep, so a
cut voice's :attr:`~rutherford.acp.session.ACPSession.partial_text` is non-empty.

To drive the WRITE/PROPOSE sandbox a test needs the agent to actually route a file write (or run a command)
through the ACP client callbacks. A ``WRITE=<path>:<content>`` token makes the agent call ``fs/write`` with
that path and content (the content runs to end-of-line, with ``\\n`` decoded to a newline), so the sandbox
path -- worktree create, diff, apply, the FileGateway path-escape guard -- is exercised without a real model.
A ``RUN=<command>`` token makes the agent spawn a terminal for that command and wait for its exit, so the
TerminalBroker (write/yolo) or its denial (read_only/propose) is exercised. The agent answers with what
happened (``wrote <path>`` / ``write denied`` / ``ran <cmd> exit <code>`` / ``terminal denied``), so a test
can assert the callback's outcome from the answer text too.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

# * Script-path launch prepends ``tests/`` to ``sys.path``; pop it so ``import acp`` hits the SDK,
# ! not the ``tests.acp`` unit-test package that would otherwise shadow it.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if sys.path and sys.path[0] == _TESTS_DIR:
    sys.path.pop(0)

import acp.schema as _acp_schema  # noqa: E402
from acp.helpers import update_agent_message_text, update_agent_thought_text  # noqa: E402
from acp.schema import (  # noqa: E402
    AgentCapabilities,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

from acp import PROTOCOL_VERSION, RequestError, run_agent  # noqa: E402

# * Legacy SessionModelState / ModelInfo exist only on ACP 0.10.x; 0.11+ removes them. Never copy protocol
# classes -- optional getattr keeps this module importable on both SDKs.
_ModelInfo = getattr(_acp_schema, "ModelInfo", None)
_SessionModelState = getattr(_acp_schema, "SessionModelState", None)
_LEGACY_SESSION_MODELS = _ModelInfo is not None and _SessionModelState is not None
_NEW_SESSION_HAS_MODELS = "models" in getattr(NewSessionResponse, "model_fields", {})
_LOAD_SESSION_HAS_MODELS = "models" in getattr(LoadSessionResponse, "model_fields", {})


def _block_text(block: Any) -> str:
    return str(getattr(block, "text", "") or "")


def _planted_answer(text: str) -> str | None:
    """The answer a ``SAY=<text>`` token dictates (the rest of that line), or ``None`` when absent.

    Capturing only to the next newline keeps the appended verdict instruction (which the consensus
    service adds on a later line, and which itself contains the literal ``VERDICT: <token>``) out of the
    planted answer, so a test that plants ``SAY=VERDICT: yes`` gets exactly ``VERDICT: yes`` back.
    """
    marker = "SAY="
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    line, _, _ = rest.partition("\n")
    return line.strip()


def _env_answer(text: str) -> str | None:
    """The agent's own value of the env var an ``ENV=<name>`` token names, or ``None`` when absent.

    Lets a test assert Rutherford propagated a lineage/depth variable (``RUTHERFORD_DEPTH`` /
    ``RUTHERFORD_LINEAGE`` / ``RUTHERFORD_PARENT_RUN``) into the spawned agent's environment: the agent
    answers with ``<name>=<value>`` (or ``<name>=(unset)`` when the variable is absent).
    """
    marker = "ENV="
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    name, _, _ = rest.partition("\n")
    name = name.strip().split()[0] if name.strip() else ""
    if not name:
        return None
    return f"{name}={os.environ.get(name, '(unset)')}"


def _sleep_seconds(text: str) -> float | None:
    """The seconds to sleep before answering: the ``RUTHERFORD_FAKE_SLEEP`` env, else a ``SLEEP=<n>`` token.

    The env var lets a test register a *slow* fake agent by descriptor (its own command/env), so a consensus
    panel can mix a fast voice and a slow voice without per-voice prompts -- the panel sends one shared prompt.
    A ``SLEEP=<n>`` token in the prompt is the alternative for a one-off slow turn (a bare ``SLEEP`` sleeps a
    long default). ``None`` when neither asks for a sleep.
    """
    env = os.environ.get("RUTHERFORD_FAKE_SLEEP")
    if env:
        try:
            return float(env)
        except ValueError:
            return 5.0
    marker = "SLEEP"
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    if rest.startswith("="):
        token, _, _ = rest[1:].partition("\n")
        try:
            return float(token.strip().split()[0])
        except (ValueError, IndexError):
            return 5.0
    # * Default hang for bare ``SLEEP`` / ``HANG`` peers: long enough for a 1s turn timeout to win,
    #   short enough that a missed cancel does not park a worker for half a minute.
    return 5.0


def _write_request(text: str) -> tuple[str, str] | None:
    """The ``(path, content)`` a ``WRITE=<path>:<content>`` token dictates, or ``None`` when absent.

    The token runs to end-of-line; the first ``:`` splits path from content (so a Windows path's drive colon
    is not a separator -- callers pass a relative path), and a literal ``\\n`` in the content is decoded to a
    real newline so a test can plant a multi-line file on one prompt line.
    """
    marker = "WRITE="
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    line, _, _ = rest.partition("\n")
    path, sep, content = line.partition(":")
    if not sep:
        return None
    return path.strip(), content.replace("\\n", "\n")


def _run_command(text: str) -> str | None:
    """The command a ``RUN=<command>`` token dictates (the rest of that line), or ``None`` when absent."""
    marker = "RUN="
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker) :]
    line, _, _ = rest.partition("\n")
    return line.strip() or None


def _verdict_env() -> str | None:
    """A fixed ``VERDICT: <token>`` answer from ``RUTHERFORD_FAKE_VERDICT``, or ``None`` when unset.

    Lets a test register a voice that always votes a chosen way by descriptor env, so a convergence-tracked
    debate (F5) can mix a steady ``yes`` voter and a steady ``no`` voter without per-voice prompts -- the
    panel sends one shared question, but each agent's own env decides its stable verdict.
    """
    value = os.environ.get("RUTHERFORD_FAKE_VERDICT")
    return f"VERDICT: {value}" if value else None


def _ranking_reply(text: str) -> str | None:
    """A deterministic ``RANK:`` ballot when the prompt is a RANK ranking round, else ``None``.

    Drives the RANK two-round protocol (F4b) without a real model: when the prompt carries the ranking
    instruction, the agent reads the ``## <LABEL>`` candidate headers and ranks them in PRESENTED order
    (top-to-bottom as shown). Since Rutherford anonymizes + shuffles each voter's ballot, presented order
    is a per-voter permutation, so the panel still exercises the de-anonymization and Borda aggregation.
    """
    if "Rank ALL of these answers" not in text:
        return None
    labels = re.findall(r"(?m)^##\s+(\S+)\s*$", text)
    if not labels:
        return None
    return "RANK: " + ", ".join(labels)


def _split_csv_respecting_brackets(raw: str) -> list[str]:
    """Split a comma-separated env list without breaking ``base[k=v,k2=v2]`` compound model ids.

    Top-level commas separate entries; commas inside a single ``[...]`` pair stay inside the entry.
    Nested / unbalanced brackets are not specially recovered -- the leftover text is still emitted.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for char in raw:
        if char == "[":
            depth += 1
            buf.append(char)
        elif char == "]":
            depth = max(0, depth - 1)
            buf.append(char)
        elif char == "," and depth == 0:
            item = "".join(buf).strip()
            if item:
                parts.append(item)
            buf = []
        else:
            buf.append(char)
    item = "".join(buf).strip()
    if item:
        parts.append(item)
    return parts


def _advertised_models() -> object | None:
    """Legacy SessionModelState from ``RUTHERFORD_FAKE_MODELS``, or ``None`` when absent / unsupported.

    Off by default so existing tests that do not expect ``session/set_model`` stay unchanged. On ACP 0.10.x
    the typed ``SessionModelState`` is built when the env is set. On ACP 0.11+ the types are gone -- this
    returns ``None`` and :func:`_advertised_config_options` surfaces the same ids on the stable model channel
    instead (no copied protocol classes).
    """
    if not _LEGACY_SESSION_MODELS:
        return None
    assert _ModelInfo is not None and _SessionModelState is not None  # narrowed by _LEGACY_SESSION_MODELS
    raw = os.environ.get("RUTHERFORD_FAKE_MODELS")
    if not raw:
        return None
    ids = _split_csv_respecting_brackets(raw)
    if not ids:
        return None
    infos = [_ModelInfo(model_id=model_id, name=model_id) for model_id in ids]
    # * getattr constructors are typed Any; cast keeps the return as object without copying protocol types.
    return cast(object, _SessionModelState(available_models=infos, current_model_id=ids[0]))


def _launch_model_from_argv() -> str | None:
    """The ``--model <id>`` value from this process argv, if Rutherford launched the fake that way.

    Distinct from a config-option echo: a launch-selected model is process intent, not proof of in-session
    selection or of runtime inference. ``None`` when the flag is absent.
    """
    try:
        index = sys.argv.index("--model")
    except ValueError:
        return None
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def _fake_models_env_values() -> list[str]:
    """Selectable model ids from ``RUTHERFORD_FAKE_MODELS``, or ``[]`` when unset."""
    raw = os.environ.get("RUTHERFORD_FAKE_MODELS")
    if not raw:
        return []
    return _split_csv_respecting_brackets(raw)


def _advertised_config_options() -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean] | None:
    """The select config options this fake advertises at ``new_session`` (from env), or ``None`` when none.

    Off by default so the existing tests, which expect no ``set_config_option`` call, are unchanged. Two opt-in
    env vars, either or both:

    * ``RUTHERFORD_FAKE_EFFORT_OPTION=<id>:<v1,v2,...>`` -- a reasoning-effort select option (F8a), e.g.
      ``reasoning_effort:low,medium,high,xhigh`` for codex, ``effort:low,medium,high,xhigh,max`` for claude_code.
    * ``RUTHERFORD_FAKE_MODEL_OPTION=<v1,v2,...>`` -- a ``model`` select option (id ``model``, category
      ``model``), the SECOND ACP model channel that claude_code's claude-agent-acp uses INSTEAD of
      ``session.models``; the values are model aliases (e.g. ``default,sonnet,haiku``). Compound ids may
      contain commas inside ``[...]``; top-level commas still separate entries.

    When legacy SessionModelState is unavailable (ACP 0.11+) and ``RUTHERFORD_FAKE_MODEL_OPTION`` is unset,
    ``RUTHERFORD_FAKE_MODELS`` is surfaced here so the fake still advertises selectable models on the stable
    channel. The first value of each is its advertised current value. Optional
    ``RUTHERFORD_FAKE_MODEL_MISMATCH=1`` makes ``set_config_option`` echo a non-matching ``current_value``
    (confirmation failure path).
    """
    advertised: list[SessionConfigOptionSelect | SessionConfigOptionBoolean] = []
    effort_raw = os.environ.get("RUTHERFORD_FAKE_EFFORT_OPTION")
    if effort_raw:
        option_id, _, values_raw = effort_raw.partition(":")
        values = [item.strip() for item in values_raw.split(",") if item.strip()]
        if option_id.strip() and values:
            advertised.append(
                SessionConfigOptionSelect(
                    id=option_id.strip(),
                    name="Effort",
                    type="select",
                    current_value=values[0],
                    options=[SessionConfigSelectOption(name=value, value=value) for value in values],
                )
            )
    model_raw = os.environ.get("RUTHERFORD_FAKE_MODEL_OPTION")
    model_values = _split_csv_respecting_brackets(model_raw) if model_raw else []
    # * ACP 0.11+: no SessionModelState -- promote RUTHERFORD_FAKE_MODELS onto the config-option channel.
    if not model_values and not _LEGACY_SESSION_MODELS:
        model_values = _fake_models_env_values()
    if model_values:
        advertised.append(
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                type="select",
                current_value=model_values[0],
                options=[SessionConfigSelectOption(name=value, value=value) for value in model_values],
                category="model",
            )
        )
    return advertised or None


def _new_session_response(*, session_id: str) -> NewSessionResponse:
    """Build ``NewSessionResponse`` with ``models`` only when the installed SDK still declares the field."""
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "config_options": _advertised_config_options(),
    }
    if _NEW_SESSION_HAS_MODELS:
        kwargs["models"] = _advertised_models()
    return NewSessionResponse(**kwargs)


def _load_session_response() -> LoadSessionResponse:
    """Build ``LoadSessionResponse`` with ``models`` only when the installed SDK still declares the field."""
    kwargs: dict[str, Any] = {"config_options": _advertised_config_options()}
    if _LOAD_SESSION_HAS_MODELS:
        kwargs["models"] = _advertised_models()
    return LoadSessionResponse(**kwargs)


class FakeAgent:
    """A deterministic ACP agent driven entirely by the prompt text."""

    def __init__(self) -> None:
        self._client: Any = None
        #: The session id a ``session/load`` resumed, so a ``WHOAMI`` prompt can prove a turn ran on a RESUMED
        #: session (vs a fresh ``session/new``). ``None`` until a load happens.
        self._loaded_session: str | None = None
        #: The effort tier a ``session/set_config_option`` set, so an ``EFFORT?`` prompt can prove Rutherford's
        #: config-option effort path reached the agent and with which (clamped) value. ``None`` until set.
        self._effort_set: str | None = None
        #: The model value a ``session/set_config_option`` set on the ``model`` config option, so a ``MODEL?``
        #: prompt can prove Rutherford's SECOND-channel model selection reached the agent. ``None`` until set.
        self._model_set: str | None = None
        #: Call counters for dual-channel routing tests (config preferred over set_model when both advertise).
        self._set_model_calls = 0
        self._set_config_model_calls = 0
        #: Model id from launch argv ``--model`` (process intent), distinct from config-option ``_model_set``.
        self._launch_model = _launch_model_from_argv()

    def on_connect(self, conn: Any) -> None:
        self._client = conn

    async def initialize(
        self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None, **kwargs: Any
    ) -> InitializeResponse:
        # Advertise the loadSession capability so the resume (session/load) path is exercisable. A test that
        # needs an agent which CANNOT resume sets RUTHERFORD_FAKE_NO_LOADSESSION=1 (then a resume -> RESUME_FAILED).
        supports_load = os.environ.get("RUTHERFORD_FAKE_NO_LOADSESSION") != "1"
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(load_session=supports_load),
        )

    async def new_session(
        self, cwd: str, additional_directories: Any = None, mcp_servers: Any = None, **kwargs: Any
    ) -> NewSessionResponse:
        return _new_session_response(session_id="fake-session-1")

    async def load_session(
        self, cwd: str, session_id: str, additional_directories: Any = None, mcp_servers: Any = None, **kwargs: Any
    ) -> LoadSessionResponse:
        # Resume: the agent reloads the named conversation. session/load keeps the requested id (no new one is
        # minted), so the client runs the next prompt under ``session_id``. Recorded so WHOAMI can confirm it.
        self._loaded_session = session_id
        return _load_session_response()

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        # Accept any advertised id on ACP 0.10.x (unstable session/set_model). On ACP 0.11+ the client no
        # longer calls this; the method stays for router compatibility when use_unstable_protocol is enabled.
        self._set_model_calls += 1
        return None

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        # Record what the config-option path set, so an EFFORT? / MODEL? prompt can echo it back -- proof the
        # value reached the agent (effort after Rutherford's clamp; model after the second-channel selection).
        if isinstance(value, str):
            if config_id == "model":
                self._model_set = value
                self._set_config_model_calls += 1
            else:
                self._effort_set = value
        # The response REQUIRES the full set of options with their updated current values (a real agent echoes
        # them back), so reflect the new current_value on the matching option rather than returning an empty set.
        # RUTHERFORD_FAKE_MODEL_MISMATCH=1 leaves model current_value stale so confirmation fails.
        options = _advertised_config_options() or []
        mismatch = os.environ.get("RUTHERFORD_FAKE_MODEL_MISMATCH") == "1"
        for option in options:
            if (
                isinstance(option, SessionConfigOptionSelect)
                and option.id == config_id
                and isinstance(value, str)
                and not (mismatch and config_id == "model")
            ):
                option.current_value = value
        return SetSessionConfigOptionResponse(config_options=options)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def prompt(
        self, prompt: list[Any], session_id: str, message_id: str | None = None, **kwargs: Any
    ) -> PromptResponse:
        text = "\n".join(_block_text(block) for block in prompt)
        if os.environ.get("RUTHERFORD_FAKE_MODEL_UNAVAILABLE"):
            # Simulate the provider rejecting the model mid-turn -- the real AWS Bedrock phrasing a Claude Code
            # seat hits when handed a bare cloud alias. Raise a RequestError so the JSON-RPC error MESSAGE (not
            # just its data) carries the marker to the client, where session.prompt maps it to ACP_TURN_ERROR
            # with this text -- which doctor classifies as ``model_unavailable`` (and, on a Bedrock host, attaches
            # the remediation hint).
            raise RequestError(-32603, "API Error (claude-opus-4-8): 400 The provided model identifier is invalid.")
        if "HANG" in text:
            await asyncio.sleep(5)
        if "REFUSE" in text:
            return PromptResponse(stop_reason="refusal")
        if "EMPTY" in text:
            return PromptResponse(stop_reason="end_turn")
        if "MODEL?" in text:
            # Report config-option selection separately from launch argv ``--model``. A config echo is not
            # runtime attestation; launch_model proves the spawn flag reached the agent process.
            await self._client.session_update(
                session_id,
                update_agent_message_text(
                    f"model={self._model_set or '(unset)'} "
                    f"launch_model={self._launch_model or '(unset)'} "
                    f"set_model_calls={self._set_model_calls} "
                    f"set_config_calls={self._set_config_model_calls}"
                ),
            )
            return PromptResponse(stop_reason="end_turn")
        if "EFFORT?" in text:
            # Report the effort tier set via session/set_config_option (or '(unset)'), so a test can prove the
            # config-option effort path reached the agent with the clamped tier.
            await self._client.session_update(
                session_id, update_agent_message_text(f"effort={self._effort_set or '(unset)'}")
            )
            return PromptResponse(stop_reason="end_turn")
        if "WHOAMI" in text:
            # Report the session this turn runs under and whether it was RESUMED (session/load) -- lets a test
            # prove a follow-up delegate with session_id actually continued the prior session, not a fresh one.
            resumed = "yes" if self._loaded_session == session_id else "no"
            await self._client.session_update(
                session_id, update_agent_message_text(f"session={session_id} resumed={resumed}")
            )
            return PromptResponse(stop_reason="end_turn")
        ranking = _ranking_reply(text)
        if ranking is not None:
            # RANK round 2 (F4b): rank the presented candidate labels in order. Checked before SAY so a
            # ranking ballot is answered deterministically rather than echoed.
            await self._client.session_update(session_id, update_agent_message_text(ranking))
            return PromptResponse(stop_reason="end_turn")
        env_answer = _env_answer(text)
        if env_answer is not None:
            # ENV=<name> makes the agent answer with the value of that environment variable in its OWN
            # subprocess environment, so a test can assert Rutherford propagated e.g. RUTHERFORD_DEPTH /
            # RUTHERFORD_LINEAGE into the spawned agent. Returns before any sleep -- it is a pure env echo.
            await self._client.session_update(session_id, update_agent_message_text(env_answer))
            return PromptResponse(stop_reason="end_turn")
        write = _write_request(text)
        if write is not None:
            outcome = await self._do_write(session_id, *write)
            await self._client.session_update(session_id, update_agent_message_text(outcome))
            return PromptResponse(stop_reason="end_turn")
        run = _run_command(text)
        if run is not None:
            ran = await self._do_run(session_id, run)
            await self._client.session_update(session_id, update_agent_message_text(ran))
            return PromptResponse(stop_reason="end_turn")
        sleep_for = _sleep_seconds(text)
        if sleep_for is not None:
            # Stream a partial answer BEFORE the long sleep, so a panel deadline that cuts this voice has a
            # harvestable partial. The cut cancels the turn during the sleep, before the final message below.
            await self._client.session_update(session_id, update_agent_message_text("partial-so-far"))
            await asyncio.sleep(sleep_for)
        answer = _planted_answer(text)
        if answer is None:
            answer = _verdict_env() or ("42" if "17 + 25" in text else f"ECHO:{text[:40]}")
        await self._client.session_update(session_id, update_agent_thought_text("thinking"))
        await self._client.session_update(session_id, update_agent_message_text(answer))
        return PromptResponse(stop_reason="end_turn")

    async def _do_write(self, session_id: str, path: str, content: str) -> str:
        """Route a ``fs/write`` through the ACP client; answer with what happened (wrote / denied / escaped)."""
        try:
            await self._client.write_text_file(content=content, path=path, session_id=session_id)
        except RequestError as exc:
            return f"write denied: {exc}"
        return f"wrote {path}"

    async def _do_run(self, session_id: str, command: str) -> str:
        """Route a terminal command through the ACP client; answer with the exit code (or the denial reason)."""
        parts = command.split()
        head, args = parts[0], parts[1:]
        try:
            created = await self._client.create_terminal(command=head, session_id=session_id, args=args)
            exit_resp = await self._client.wait_for_terminal_exit(
                session_id=session_id, terminal_id=created.terminal_id
            )
            output = await self._client.terminal_output(session_id=session_id, terminal_id=created.terminal_id)
            await self._client.release_terminal(session_id=session_id, terminal_id=created.terminal_id)
        except RequestError as exc:
            return f"terminal denied: {exc}"
        return f"ran {command} exit {exit_resp.exit_code} output {output.output.strip()[:80]}"


async def _main() -> None:
    # session/set_model is an unstable ACP method on 0.10.x; enable when the installed SDK still accepts the flag.
    # On 0.11+ the flag may remain for forward compatibility but the method is gone -- do not invent a shim.
    import inspect

    kwargs: dict[str, Any] = {}
    if "use_unstable_protocol" in inspect.signature(run_agent).parameters:
        kwargs["use_unstable_protocol"] = True
    await run_agent(FakeAgent(), **kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":
    asyncio.run(_main())
