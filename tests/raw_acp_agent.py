# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""A RAW-WIRE ACP agent for tests: hand-authored JSON-RPC frames, with the SDK deliberately absent.

``tests/fake_acp_agent.py`` cannot exercise a malformed payload, and that is structural rather than an
oversight worth fixing there. It builds SDK pydantic models and hands them to ``acp.run_agent``, so producer
and consumer share one codec: every frame it emits is, by construction, a frame the installed SDK's own
serializer considered valid. ACP 0.12.0's lenient deserialization -- salvage-on-error for a field that fails
validation, skip-invalid-items for a list entry that does -- fires only on payloads that serializer will
never emit. A test written on top of that fixture is therefore incapable of reaching the new leniency, which
is why a fully green suite said nothing at all about the 0.11 -> 0.12 bump.

This module is the other half of the pair. It speaks ACP's newline-delimited JSON-RPC framing directly on
stdin/stdout using nothing but ``json`` and ``sys``, so a test can put ANY literal bytes on the wire -- an
``agentCapabilities`` that is a bare string, a config option whose ``category`` is an object, a
``configOptions`` array carrying one item the schema rejects -- and then watch what Rutherford's real
client, real transport, real handshake and real teardown do with it. Nothing is stubbed on the Rutherford
side, so an assertion here is about production behaviour rather than about a mock.

Driven by two argv paths, both written by the calling test:

* ``<script.json>`` -- ``{"results": {<method>: <raw result>}, "notify_before": {<method>: [<params>...]},
  "drop": [<method>, ...]}``. Only the malformed bit needs authoring; every method a script leaves out is
  answered from :data:`_DEFAULT_RESULTS`, so the literal JSON in a test stays down to the few lines it is
  actually about, and ``drop`` removes a default for the one test that is about an unanswerable method. A
  scripted result is emitted verbatim -- never validated, normalized or round-tripped here, because the
  entire value of this fixture is that the bytes on the wire are exactly the bytes the test wrote.
* ``<transcript.jsonl>`` -- one ``{"method": ..., "params": ...}`` line per request received, appended in
  arrival order. This is how a test proves a NEGATIVE that is invisible from the client side: that
  Rutherford never issued ``session/set_config_option`` at all and therefore let the turn run on whatever
  default the agent already had.

Two deliberate properties, both load-bearing for the tests that use this:

* The loop is serial and stdlib-only. Rutherford never has two requests outstanding, so serial is correct,
  and sharing any code with the client would reintroduce the shared-codec blind spot this exists to close.
* It exits only on stdin EOF, never on its own initiative. A test asserting the agent was NOT orphaned
  needs the process to stay up until something tears it down; an agent that exited by itself would make
  that assertion pass for the wrong reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: The session id the default ``session/new`` mints. A script that overrides ``session/new`` with a
#: different id must override ``notify_before`` too, since the default notification below carries this one.
#: Harmless if it does not: ``sessionId`` is only shape-checked on the way in, and the journal keys on the
#: update kind rather than on the session -- but a test that means to be realistic should keep them in step.
SESSION_ID = "raw-session-1"

#: The answer for every method a script does not override -- a healthy, minimal, schema-valid agent. Having
#: a default for each means a script contains ONLY the malformed payload under test, so a reader sees the
#: defect immediately instead of hunting it inside a full transcript of well-formed noise.
_DEFAULT_RESULTS: dict[str, Any] = {
    "initialize": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}},
    "session/new": {"sessionId": SESSION_ID},
    "session/load": {},
    "session/set_config_option": {"configOptions": []},
    "session/prompt": {"stopReason": "end_turn"},
}

#: Notifications emitted immediately BEFORE the response to a given method. The prompt default streams one
#: message chunk so a turn that is not about the answer text never fails as ACP_EMPTY_ANSWER by accident --
#: that failure mode is real, and a test tripping it would report the wrong defect.
_DEFAULT_NOTIFY_BEFORE: dict[str, list[Any]] = {
    "session/prompt": [
        {
            "sessionId": SESSION_ID,
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "raw-ok"}},
        }
    ]
}


def _write(message: dict[str, Any]) -> None:
    """Put one JSON-RPC frame on stdout in the newline-delimited framing ACP's stdio transport reads."""
    sys.stdout.buffer.write(json.dumps(message).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _record(transcript: Path, method: str, params: Any) -> None:
    """Append one received request to the transcript, reopening each time so a killed agent loses nothing.

    Opened and closed per line rather than held for the process lifetime because most tests here end by
    tearing the agent down mid-conversation; a buffered handle would take the last few calls with it, and
    those are usually the ones the assertion is about.
    """
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"method": method, "params": params}) + "\n")


def _dispatch(
    message: dict[str, Any],
    *,
    results: dict[str, Any],
    notify_before: dict[str, list[Any]],
    transcript: Path,
) -> None:
    """Record one incoming message, emit its scripted notifications, and answer it when it wants an answer."""
    method = message.get("method")
    if not isinstance(method, str):
        return  # a response to a request we sent, and this agent sends none
    _record(transcript, method, message.get("params"))
    for params in notify_before.get(method, []):
        _write({"jsonrpc": "2.0", "method": "session/update", "params": params})
    if "id" not in message:
        return  # a notification (session/cancel) takes no reply
    if method not in results:
        # Answer rather than ignore. An unscripted method left unanswered parks the client on its full
        # handshake budget and surfaces ~30s later as a timeout, which reads as a hang in the harness
        # instead of as the one-line script omission it actually is.
        _write(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"raw_acp_agent has no script entry for {method!r}"},
            }
        )
        return
    _write({"jsonrpc": "2.0", "id": message["id"], "result": results[method]})


def main(argv: list[str]) -> int:
    """Read newline-delimited JSON-RPC from stdin until EOF, dispatching each frame."""
    script: dict[str, Any] = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    transcript = Path(argv[2])
    results: dict[str, Any] = {**_DEFAULT_RESULTS, **script.get("results", {})}
    for dropped in script.get("drop", []):
        results.pop(dropped, None)
    notify_before: dict[str, list[Any]] = {**_DEFAULT_NOTIFY_BEFORE, **script.get("notify_before", {})}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return 0
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except ValueError:
            continue  # the client never sends malformed JSON; tolerate rather than die mid-conversation
        if isinstance(message, dict):
            _dispatch(message, results=results, notify_before=notify_before, transcript=transcript)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
