# Architecture

Rutherford is a stdio MCP server that orchestrates other agentic coding agents over the
[Agent Client Protocol (ACP)](https://agentclientprotocol.com). Its three orchestration operations are
**delegate** (hand one task to one agent), **consensus** (hand the same task to several agents in
parallel), and **debate** (have several agents argue across rounds on persistent sessions). It never
calls a model provider API directly and never reimplements an agent's own features.

The defining fact: Rutherford is the ACP *client* and each coding agent is an ACP *agent*. It spawns
the agent as an ACP server over stdio and drives a real `initialize` / `new_session` / `prompt`
exchange. Under ACP the protocol negotiates output, system prompts, file context, permissions, and
resume as structured messages, so there is no per-agent stdout parser — the v2 subprocess-adapter
model is gone.

**Non-goals.** Rutherford does not manage agent authentication, does not store conversation history
itself, and does not implement any coding-agent behavior. It is a transport and orchestration layer,
not an agent.

---

## Layer diagram

```
MCP tool layer     src/rutherford/server.py + tools/
                   FastMCP @mcp.tool wrappers; validate input, call a service,
                   return tool_success / tool_error; no orchestration logic here.
        |
services           src/rutherford/services/
                   delegation.py  -- single agent, one ACP turn, the safety gate
                   consensus.py   -- fan the prompt out to N agents in parallel
                   debate.py      -- N agents, persistent sessions, multi-round
                   jobs.py        -- in-memory background-job store
                   roles.py       -- role persona loader and store
        |
ACP runtime        src/rutherford/acp/
                   descriptors.py -- AgentDescriptor + DescriptorRegistry + HIGH_FIDELITY
                   roster.py      -- build_registry(): built-ins + config + local detect
                   session.py     -- ACPSession, run_acp_turn (the core primitive)
                   journal.py     -- EventJournal (event-sourced turn record)
                   client.py      -- the ACP client callbacks (permission, fs, terminal)
                   permission.py  -- PermissionPolicy (safety mode -> ACP decisions)
                   launch.py      -- cross-platform launch resolution (Windows npm shims)
                   teardown.py    -- reap an agent's orphaned descendant process tree
                   conformance.py -- the doctor probe (does an agent really drive?)
                   local_detect.py-- zero-config Ollama / LM Studio detection
        |
domain + config    src/rutherford/domain/   models, enums, errors, error_codes
                   src/rutherford/config/   schema, loader, acp_json
                   src/rutherford/io/       serialize.py (TOON seam)
```

Dependencies point inward. The domain layer imports nothing from any other layer. The ACP runtime
imports from domain and config. Services import from the ACP runtime and domain. The tool layer
imports only from services, the ACP runtime (for the registry), and domain. Nothing in the core
imports a concrete agent by name; all agent access goes through the descriptor registry.

---

## The agent descriptor and registry

`acp/descriptors.py` defines `AgentDescriptor`, the small frozen declaration that replaces a
hand-written subprocess adapter:

| field | meaning |
| --- | --- |
| `id` | the agent id callers use (`goose`, `claude_code`, ...) |
| `display_name` | the human label |
| `command` | the argv that launches this agent as an ACP server (e.g. `("goose", "acp")`) |
| `provider` | the fixed model vendor when known, else `None` for bring-your-own-model |
| `env_passthrough` | which inherited env vars to pass through; `None` passes the full environment |
| `default_model` | the model used when a call names none; `None` means the agent's own default |
| `handshake_timeout_s` | seconds allotted for `initialize` + `new_session` before it is judged failed |
| `env_overrides` | env vars to set for the subprocess (e.g. a local-runtime provider env) |

`DescriptorRegistry` is an immutable id → descriptor mapping with fail-fast lookup, mirroring the v2
adapter registry's closed-mapping contract. `HIGH_FIDELITY` is the built-in roster. There is
no per-agent code: a descriptor plus the shared ACP runtime is the whole integration.

`acp/roster.py:build_registry(config)` assembles the live registry in precedence order:

1. The built-in `HIGH_FIDELITY` descriptors.
2. Auto-detected local-model agents (lowest precedence — a built-in or explicit config of the same id
   always wins), when `auto_detect_local_models` is on.
3. Config `[agents.<id>]` entries: override a built-in's fields, disable one with `enabled = false`,
   define a brand-new agent, or clone a built-in with `base` and point it at a local runtime via
   `backend`.
4. The `enabled_agents` allowlist filter, when set.

See [adding-an-agent.md](adding-an-agent.md) for the config-driven workflow.

---

## The ACP session: the core primitive

`acp/session.py` defines `ACPSession`, a live conversation with one agent: open once, run many prompt
turns, close. This is what makes a debate possible — the old subprocess model re-spawned and re-sent
the whole transcript every round; here each voice holds one session, so a later round sends only the
delta and the agent remembers its own prior reasoning in-session.

`ACPSession.open()`:

1. Resolves the launch argv for the platform (`launch.py` — see below) and spawns the agent as an ACP
   server with a clean stdio transport (the stdin read limit is raised from asyncio's 64 KiB default
   to 16 MiB so a large `session/update` does not drop the connection).
2. Performs `initialize` and `new_session(cwd, mcp_servers=[])`, each bounded by the descriptor's
   handshake timeout. ACP requires an absolute `cwd`, so it is resolved once here.
3. A spawn failure raises `ACPHandshakeError(ACP_SPAWN_FAILED, ...)`; a handshake failure raises
   `ACPHandshakeError(ACP_HANDSHAKE_FAILED, ...)`. Both are pre-prompt, so re-execution-safe.

`ACPSession.prompt(text, timeout_s)` runs one turn on the live session and reduces it to a normalized
`DelegationResult`. It never raises for an operational failure — a timeout, refusal, empty answer, or
transport error becomes a failed result carrying the ACP error code and a re-execution-safety
classification. The result reduction reads everything from the turn's event journal (below), never
from raw stdout.

`run_acp_turn(...)` is the one-shot wrapper (open, one turn, close) used by `delegate` and each
`consensus` voice. `ACPSession.close()` tears the connection down and reaps the agent's orphaned
descendant process tree (`teardown.py`): a wrapper adapter spawns the underlying CLI as a child, and
the SDK transport terminates only the direct child, so the descendants are snapshotted before
teardown and reaped after.

---

## The event journal

`acp/journal.py` defines `EventJournal`, the event-sourced record of one prompt turn. A synchronous
stream observer (registered on the ACP transport) appends a `JournalEvent` for each incoming
`session/update` notification in receive order, alongside the client's own decisions (permission
grants, fs reads/writes, terminal denials). Because the observer is synchronous and inline in the
SDK's read loop, the journal is complete the moment the prompt response resolves the turn.

The reducer derives the normalized result from the journal:

- `message_text()` — the answer, every `agent_message_chunk` concatenated in order.
- `usage()` — the latest reported token usage as a `Cost`.
- `tool_call_count()` — distinct tool calls the agent reported (the real topology, not a process
  count).
- `saw_side_effect()` / `saw_tool_activity()` — whether a served `fs/write` or terminal ran, and
  whether any tool call or permission request happened. These classify how unsafe a re-run is after
  the prompt was accepted (`ReexecutionSafety`).

---

## The permission engine

`acp/permission.py` defines `PermissionPolicy`, the safety mode rendered as ACP decisions. Rutherford
is the permission authority at the moment of each tool call:

- Reads are always served — the answer needs to see the code.
- Filesystem writes and terminal execution are allowed only in `write` / `yolo`.
- A tool-call permission request is granted in `write` / `yolo` and rejected otherwise. `select_permission`
  picks the agent's `allow_*` option for a mutating mode (preferring the one-shot `_once` form) or a
  `reject_*` option otherwise, declining the tool call rather than cancelling the whole turn.

This governs what the agent routes through ACP. An OS-level sandbox (worktree isolation) is a later
layer, so the optional `verify_read_only` git check still belongs above this for defense in depth.

---

## Request flow: a single delegate call

```
MCP client calls delegate(cli="claude_code", prompt="...", safety_mode="read_only")
        |
        v
delegate_tool (tools/delegate.py)
  ensure_known_agent, resolve_safety_mode, resolve_run_mode
  apply_role (prepend a named role's persona to the prompt)
  build DelegationRequest(target=Target(cli, model), ...)
        |
        v
DelegationService.delegate (services/delegation.py)
  1. registry.has(cli)            -> UNKNOWN_TARGET if absent
  2. is_mutating(safety_mode)     -> trusted-workspace gate for write/yolo (WORKSPACE_NOT_TRUSTED)
  3. resolve cwd, timeout, files; build PermissionPolicy
  4. run_acp_turn(descriptor, prompt, policy, cwd, timeout, model)
        |
        v
tool_success(result)  ->  encode(result)  ->  TOON text block returned to the MCP client
```

Operational failures (unknown id, spawn failure, handshake failure, timeout, refusal, empty answer,
transport error) become a structured `DelegationResult(ok=False, error=ErrorInfo(code, message))`
rather than an exception, so a consensus panel never aborts on one bad voice.

---

## Consensus and debate

**Consensus** (`services/consensus.py`) fans the prompt out to every target concurrently, each as its
own one-shot ACP session via the delegation service, and returns every voice. One failing voice is a
failed `DelegationResult` in the result, never an aborted panel. The per-call `max_targets` cap bounds
the fan-out.

**Debate** (`services/debate.py`) opens one persistent `ACPSession` per voice in parallel, then runs
up to `rounds` rounds:

- Round 1 sends each voice the full question (optionally with a stance). Later rounds send each voice
  only the delta — the other voices' latest positions — and ask it to critique and revise; its own
  session remembers its prior answer.
- A voice that fails a round drops out of the active set. The debate stops early once fewer than two
  voices remain to argue.
- An optional closing synthesis (on by default) runs a final read-only pass over the last usable
  round, performed by a named `judge` target (ideally a non-participant) or the first surviving
  voice's agent on a fresh session. The result records `synthesis_by`.
- Two voices that share a `(cli, model)` get distinct seat ids and disambiguated transcript labels
  (`goose`, `goose#2`), so their positions never merge.

All sessions are always closed at the end, even on an exception.

---

## The result envelope and the TOON seam

Every turn reduces to a `DelegationResult`:

| field | type | notes |
| --- | --- | --- |
| `target` | `Target` | the `(cli, model)` pair that answered |
| `ok` | `bool` | `True` on success, `False` on any failure |
| `text` | `str` | the clean final answer (empty on failure) |
| `duration_s` | `float` | wall-clock seconds, rounded to milliseconds |
| `session_id` | `str \| None` | the agent's ACP session id, for provenance / a later resume |
| `cost` | `Cost \| None` | token counts where the agent reported usage |
| `provenance` | `Provenance \| None` | provider, model, and a `confirmed` flag |
| `partial` | `str \| None` | streamed answer text preserved when a turn was cut at its timeout |
| `error` | `ErrorInfo \| None` | structured error on failure; `None` on success |
| `safety_mode` | `SafetyMode` | echoes the mode the turn ran under |

`ErrorInfo` carries a `code` from `domain/error_codes.py` (stable `StrEnum` members, never renamed), a
human `message`, optional `details`, and a `reexecution_safety` classification. Clients may switch on
`error.code`.

The tool layer calls `tool_success(data)` / `tool_error(code, message)` from `context.py`. Both funnel
through `io/serialize.py:encode()`, which converts pydantic models to plain data and passes the result
to the `toon` encoder (`python-toon >= 0.1.3`, imported as `from toon import encode`). This is the
single swap point for the serialization format. The PyPI packages `toon-format` and `toon-encoder` are
not used.

---

## ACP error codes and re-execution safety

The ACP transport contributes a focused set of stable codes (all in `domain/error_codes.py`):

| code | meaning | re-execution-safe? |
| --- | --- | --- |
| `ACP_SPAWN_FAILED` | the agent subprocess could not be launched | yes (pre-prompt) |
| `ACP_HANDSHAKE_FAILED` | `initialize` / `new_session` failed | yes (pre-prompt) |
| `ACP_TURN_TIMEOUT` | the prompt turn exceeded its timeout; the session was cancelled | no |
| `ACP_REFUSED` | the agent ended the turn by refusing | no |
| `ACP_EMPTY_ANSWER` | the agent ended cleanly with no answer text | no |
| `ACP_TURN_ERROR` | an error surfaced after the prompt was accepted | no (ambiguous) |

`ReexecutionSafety` is distinct from "did a side effect happen": after a `session/prompt` is accepted,
a turn can be filesystem-clean yet still unsafe to silently re-run, because cost may have accrued. Only
a pre-prompt `SAFE` failure may enter a retry or cross-agent fallback path.

---

## Launch resolution

`acp/launch.py` resolves an agent's launch command to a clean, directly-launchable process. The
problem it solves is Windows: `asyncio.create_subprocess_exec` runs a real executable, but a Windows
npm shim is not one. A `.cmd` shim launched via `cmd /c` and a `.ps1` shim via PowerShell both corrupt
the raw JSON-RPC stdin the ACP transport needs. So for an npm shim, `prepare_argv` parses the shim and
launches its real target (the bundled `.exe`, or `node <entry>.js`) directly with clean stdio. A
non-npm shim falls back to the `.ps1` sibling via PowerShell, then `cmd /c`. On non-Windows platforms
the resolved executable is launched directly.

---

## Conformance probing (doctor)

`acp/conformance.py` backs the `doctor` tool. Each descriptor is probed with a trivial read-only ACP
turn in an isolated temp directory (so a conformance check never triggers an agent's heavyweight
workspace setup against the real repo). The probe classifies the outcome:

| status | meaning |
| --- | --- |
| `ok` | handshake succeeded and the agent answered |
| `no_answer` | handshake succeeded but the agent answered empty or refused |
| `handshake_failed` | installed, but `initialize` / `new_session` failed |
| `not_installed` | the launch command was not found |
| `error` | some other failure |

This is the only trustworthy health signal for an ACP agent, since there is no cheap non-interactive
auth check. `capabilities` is the cheap snapshot (just the registry); `doctor` makes a real call per
agent.

---

## Cross-references

- Configuration reference and file discovery: [configuration.md](configuration.md)
- Adding or configuring an agent: [adding-an-agent.md](adding-an-agent.md)
- Local models (Ollama / LM Studio): [local-models.md](local-models.md)
- The safety model and the permission engine: [security.md](security.md)
