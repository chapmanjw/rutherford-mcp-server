# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Commands

```sh
uv sync                       # install deps (project + dev group) into .venv
uv run ruff check .           # lint
uv run ruff format .          # format (write)
uv run ruff format --check .  # format check (CI mode)
uv run mypy                   # type-check (strict)
uv run python scripts/check_license_headers.py   # license-header check
uv run pytest                 # unit tests only (integration deselected by default)
uv run pytest -m integration  # local-only suite that drives real ACP agents
```

A `justfile` wraps these. `just check` runs lint + format-check + license-check + typecheck +
unit tests + the per-file coverage floor + the entrypoint smoke check (the pre-push gate);
`just test-integration` drives the real agents. Run a single test file with
`uv run pytest tests/test_session.py`.

Recommended pre-push flow: `just check`, then `just test-integration` for whatever agents the
machine has installed and authenticated.

## Architecture

Rutherford is a stdio MCP server that orchestrates other agentic coding agents over the
[Agent Client Protocol (ACP)](https://agentclientprotocol.com). It is the ACP *client*; each coding
agent is an ACP *agent* that Rutherford spawns as a server over stdio and drives through a real
`initialize` / `new_session` / `prompt` exchange. It never calls a model provider's API directly and
never reimplements an agent's features — under ACP the protocol negotiates output, system prompts,
file context, permissions, and resume, so there is no per-agent output parser to maintain.

This is a complete rewrite of the v2 design. There is no `ProcessRunner`, no `adapters/` package, no
`build_invocation` / `parse_output`, and no hand-written code adapter per CLI. Adding an agent is now
config-driven.

Layered, with dependencies pointing inward toward the domain:

```
MCP tool layer (FastMCP)   src/rutherford/server.py + tools/   thin wrappers, no business logic
        |
services (orchestration)   services/   delegation, consensus, debate, jobs, roles
        |
ACP runtime                acp/   session, journal, permission, descriptors, roster, conformance
        |
domain + config            domain/, config/, io/   models, enums, errors, error codes, config
```

### The key seams

- **`AgentDescriptor` / `DescriptorRegistry` (`acp/descriptors.py`)** — a descriptor is the small
  declaration that replaces a subprocess adapter: an id, a display name, the launch `command` (the
  argv that starts the agent as an ACP server), an optional fixed `provider`, a `default_model`, a
  handshake budget, and env overrides. `HIGH_FIDELITY` is the built-in roster. The registry
  is a closed, fail-fast id → descriptor mapping.

- **`ACPSession` / `run_acp_turn` (`acp/session.py`)** — the reusable connection primitive. An
  `ACPSession` spawns the agent, performs the handshake, and runs any number of `session/prompt`
  turns on the *same* live session (the foundation for a debate: one session per voice across all
  rounds, sending only the delta each round). Each turn reduces its event journal into a normalized
  `DelegationResult` and classifies the failure's re-execution safety. `run_acp_turn` is the one-shot
  open-prompt-close wrapper used by `delegate` and `consensus`.

- **`EventJournal` (`acp/journal.py`)** — the event-sourced record of one turn. A synchronous stream
  observer appends each incoming `session/update` (and the client's own permission / fs decisions) in
  receive order, so the journal is complete the moment the prompt response resolves. The answer text,
  token usage, tool activity, and side-effect signal are all *derived* from the journal, never scraped
  from stdout.

- **`PermissionPolicy` (`acp/permission.py`)** — the safety mode rendered as ACP permission /
  filesystem / terminal decisions. Rutherford is the permission authority at each tool call: a
  non-mutating mode serves reads but denies writes, terminal execution, and tool-permission requests;
  a mutating mode allows them. It selects the one-shot allow/reject permission option the agent offers.

- **The config-driven roster (`acp/roster.py`)** — `build_registry(config)` assembles the live
  registry: the built-in descriptors, then any auto-detected local-model agents (lowest precedence),
  then config overrides / additions, then the `enabled_agents` filter. An `[agents.<id>]` entry
  overrides a built-in, defines a new agent, or clones a built-in with `base` to point it at a local
  runtime via `backend`.

### Layering rules

- The FastMCP layer is thin: a tool validates input, calls a service, and returns the normalized
  envelope via `tool_success` / `tool_error`. No orchestration logic lives there. `server.py` declares
  the `@mcp.tool` surface; each `tools/<name>.py` is the validating wrapper.
- The services depend on the descriptor registry and the ACP runtime, never on a concrete agent. The
  whole core is testable with the fake ACP agent in `tests/fake_acp_agent.py` and no real subprocess.
- Adding an agent is config (`[agents.<id>]`) or a new built-in `AgentDescriptor` — never a code
  adapter. See [docs/adding-an-agent.md](docs/adding-an-agent.md).
- Default `SafetyMode` is `read_only`; `write` and `yolo` are explicit opt-in behind a
  trusted-workspace check.

### Conventions

- Python 3.11+, fully type-annotated, mypy strict. Ruff for lint and format (120-char lines).
- A two-line SPDX license header on every source file (enforced by
  `scripts/check_license_headers.py`).
- Docstrings on the public API of the core layers (acp, services, domain).
- Tool payloads are serialized as TOON behind the `io/serialize.py` seam (`python-toon`).
- No emojis in source files unless a user-visible string clearly benefits.
