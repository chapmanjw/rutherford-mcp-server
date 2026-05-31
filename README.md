<p align="center">
  <img src="docs/images/logo.png" width="200" alt="Rutherford logo">
</p>

# Rutherford

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets one AI coding
CLI delegate work to, and build consensus across, a crew of others. Rutherford runs other agentic
coding CLIs (Claude Code, Codex, Antigravity, Kiro, OpenCode, Goose) as headless subprocesses and
returns each one's answer in a single normalized envelope. It is CLI-only: it orchestrates terminal
coding agents and never calls a model provider API directly.

```
.---------.
|  \/\/\/ |
|  O  [==]|
|    <    |
|  \___/  |
'---------'
-- Ensign Sam Rutherford --
USS Cerritos . Engineering
```

## Why Rutherford?

> Rutherford is named for Ensign Sam Rutherford, the irrepressibly cheerful engineer aboard the
> USS Cerritos in Star Trek: Lower Decks. Rutherford has a cybernetic implant and a gift for
> getting heterogeneous systems to cooperate, which is exactly what this server does: it lets one
> AI coding agent hand work to a crew of others and bring their results back. Like the show's
> lower-deckers, Rutherford does the unglamorous coordination so the bridge, your primary agent,
> gets the win.
>
> Star Trek and Lower Decks are trademarks of their respective owners. This is an unaffiliated,
> fan-named open-source project and implies no endorsement.

## Experimental status

Rutherford drives independent third-party CLIs. Their headless flags, output formats, and auth
mechanisms change between releases, and a CLI update can change or remove something an adapter
relies on. Every flag in this repo was verified against the CLI's own `--help` and docs on the
date in the table below; pin your CLI versions, re-verify after upgrades, and treat the integration
as evolving. Each adapter keeps all of its CLI-specific details in one file, so a change is a
one-file edit.

## How it works

Rutherford is a stdio MCP server. Any MCP client -- a coding CLI or a desktop app -- calls it over
MCP, and it spawns the target CLIs as fresh, isolated headless subprocesses.

```
   any MCP client (Claude Code, Claude Desktop, Cursor, Codex, ...)
        |
        |  MCP over stdio
        v
   rutherford-mcp-server
        |  argv list, no shell   (read_only by default; depth-bounded)
        +--> claude -p "..." --output-format json
        +--> codex exec --json            (prompt on stdin)
        +--> agy -p "..."                 (answer read from the transcript file)
        +--> kiro-cli chat --no-interactive "..."
        +--> opencode run --format json "..."
        +--> goose run -t "..." --no-session
```

A self-invocation is supported and explicit: when the calling CLI targets its own adapter (Claude
Code asking Rutherford to delegate to `claude_code`), Rutherford spawns a separate headless process
that is independent of the caller's session. A delegation depth is tracked and propagated through
`RUTHERFORD_DEPTH`, so a CLI-calls-itself chain stops at a configured maximum rather than recursing
without bound.

## Supported CLIs

Invocation flags verified 2026-05-30. "(docs)" means verified against the CLI's documentation
rather than a local install.

| CLI | Adapter id | How Rutherford invokes it headlessly | Auth | Verified |
| --- | --- | --- | --- | --- |
| Claude Code | `claude_code` | `claude -p "<prompt>" --output-format json` | subscription/OAuth login or `ANTHROPIC_API_KEY` | 2026-05-30 |
| Codex | `codex` | `codex exec --json --skip-git-repo-check` (prompt on stdin) | ChatGPT login or `OPENAI_API_KEY` | 2026-05-30 |
| Antigravity | `antigravity` | `agy -p "<prompt>"` (answer read from the transcript file) | Google account login (no `whoami`; `doctor` verifies it with a live check) | 2026-05-30 |
| Kiro | `kiro` | `kiro-cli chat --no-interactive "<prompt>"` | `KIRO_API_KEY` (Pro/Pro+/Power) or `kiro-cli login` | 2026-05-30 |
| OpenCode | `opencode` | `opencode run --format json -q "<prompt>"` | provider key or `opencode auth login` | 2026-05-30 (docs) |
| Goose | `goose` | `goose run -q -t "<prompt>" --no-session` | `GOOSE_PROVIDER` + provider key | 2026-05-30 (docs) |

Antigravity's print-mode model is fixed (no model selector). Codex on Windows installs as an npm
shim, which Rutherford launches via `cmd.exe` automatically while still passing arguments as a list.
A seventh, well-behaved CLI can be added without code -- see [docs/adding-a-cli.md](docs/adding-a-cli.md).

## Using Rutherford

Rutherford's tools are called by your MCP client. From an agent you phrase a request in plain
language ("ask Codex and Claude the same question and compare them") and the agent fills in the
arguments; the examples below show those arguments as JSON, plus the TOON envelope Rutherford
returns. Every delegating tool defaults to `read_only` and to each adapter's default model.

### Delegate one task to one CLI

`delegate` is the foundational primitive: one `(cli, model)`, one prompt, one normalized result.

```json
{ "cli": "claude_code", "prompt": "In one sentence, what is the capital of France?" }
```

Returns the normalized envelope (TOON):

```
target:
  cli: claude_code
ok: true
exit_code: 0
text: The capital of France is Paris.
duration_s: 6.1
session_id: 5f3b9c1a-2e7d-4a8b-9c6e-1d2f3a4b5c6d
```

Pick a specific model (see `capabilities` for each CLI's list):

```json
{ "cli": "kiro", "model": "claude-haiku-4.5", "prompt": "Explain this regex: ^\\d{3}-\\d{4}$" }
```

Give it a workspace and files, and steer it with a role:

```json
{ "cli": "claude_code", "role": "security", "working_dir": "/abs/path/to/repo",
  "files": ["src/auth.py"], "prompt": "Audit this file for auth and injection issues." }
```

Continue a prior conversation by passing back the `session_id` from an earlier result (on CLIs
that support resume):

```json
{ "cli": "claude_code", "session_id": "5f3b9c1a-2e7d-4a8b-9c6e-1d2f3a4b5c6d",
  "prompt": "Now add error handling to that function." }
```

Let it modify the workspace -- `write`/`yolo` require both the explicit mode and a trusted
workspace (an allowlisted path or this per-call confirmation):

```json
{ "cli": "codex", "working_dir": "/abs/path/to/repo", "safety_mode": "write",
  "trust_workspace": true, "prompt": "Add a docstring to every public function in utils.py." }
```

Run a long task in the background -- `mode: "async"` returns a job id immediately:

```json
{ "cli": "opencode", "mode": "async", "working_dir": "/abs/path/to/repo",
  "prompt": "Refactor the data layer to use the repository pattern." }
```

```
job_id: 95eab04a69254959956e26fc6a1b154c
status: pending
kind: delegate
```

### Build consensus across several CLIs

`consensus` asks the same prompt of every target in parallel and returns one voice per target. One
failing voice never aborts the panel.

```json
{ "targets": [ { "cli": "claude_code" }, { "cli": "codex" },
               { "cli": "opencode", "model": "anthropic/claude-sonnet-4-6" } ],
  "prompt": "Is a message queue overkill for a single-server app? Answer in one paragraph." }
```

```
voices[3]:
  - target:
      cli: claude_code
    ok: true
    text: For a single server, a queue is usually overkill ...
  - target:
      cli: codex
    ok: true
    text: It depends on durability needs; an in-process ...
  - target:
      cli: opencode
      model: anthropic/claude-sonnet-4-6
    ok: true
    text: A queue adds operational overhead ...
```

Steer each voice with a stance (the list is parallel to `targets`):

```json
{ "targets": [ { "cli": "claude_code" }, { "cli": "codex" } ],
  "prompt": "Should we migrate this service to async I/O?", "stances": ["for", "against"] }
```

Ask Rutherford to synthesize the voices server-side (off by default, so you usually synthesize them
in your own agent):

```json
{ "targets": [ { "cli": "claude_code" }, { "cli": "kiro" } ],
  "prompt": "Best database for an append-heavy event log?", "synthesize": true }
```

The result adds a `synthesis` field alongside `voices`.

### Review code

`review` is read-only and uses the `codereviewer` role. Give it a diff:

```json
{ "targets": [ { "cli": "claude_code" }, { "cli": "codex" } ],
  "diff": "--- a/parser.py\n+++ b/parser.py\n@@\n-    return data\n+    return data or {}\n" }
```

or files for the agents to read:

```json
{ "targets": [ { "cli": "claude_code" } ], "working_dir": "/abs/path/to/repo",
  "paths": ["src/server.py", "src/runtime/process.py"] }
```

### Plan

`plan` asks one target for an ordered implementation plan, using the `planner` role:

```json
{ "cli": "claude_code", "working_dir": "/abs/path/to/repo",
  "goal": "Add OAuth2 device-code login to the CLI." }
```

### Inspect the crew

`capabilities` (no arguments) is the cheap snapshot -- every CLI, installed state, auth, and models,
with no model calls. A CLI with no non-interactive auth check (Antigravity) shows auth `unknown`
here.

`doctor` (no arguments) is the thorough health check: it confirms each CLI is installed and, by
default, verifies any adapter that is still `unknown` with a minimal real round trip -- so
Antigravity comes back `authenticated` rather than `unknown`. Pass `{ "live": false }` for a
metadata-only run with no model calls.

```json
{ "live": false }
```

### Background jobs

After an async call returns a `job_id`, poll it:

```json
{ "job_id": "95eab04a69254959956e26fc6a1b154c" }
```

`job_status` returns the status and any progress; `job_result` returns the finished
`DelegationResult` / `ConsensusResult` (or a still-running notice).

### Roles

`list_roles` (no arguments) returns the bundled personas -- `planner`, `codereviewer`, `security`,
`debugger`. Pass `"role": "<name>"` to `delegate`, `consensus`, or `review` to prepend that persona,
or add your own markdown files via the `role_dirs` config (see
[docs/configuration.md](docs/configuration.md)).

### Self-invocation

Any CLI can target its own adapter. From Claude Code, delegating to `claude_code` spawns a fresh,
isolated `claude -p` subprocess distinct from your session:

```json
{ "cli": "claude_code", "prompt": "You are a separate reviewer. Critique this approach: ..." }
```

The depth guard (`max_depth`, default 3) and the per-call target cap keep any self-referential chain
bounded.

## Install

Rutherford is a Python 3.11+ package. Install it as a tool so the `rutherford-mcp-server` command
is on your PATH:

```sh
uv tool install rutherford-mcp-server
# or: pipx install rutherford-mcp-server
# or: pip install rutherford-mcp-server
```

From source (for development):

```sh
git clone https://github.com/chapmanjw/rutherford-mcp-server
cd rutherford-mcp-server
uv sync
uv run rutherford-mcp-server --smoke   # prints a readiness line and exits
```

Rutherford does not install or authenticate the target CLIs. Install and log in to whichever CLIs
you want to orchestrate (see [docs/integration-testing.md](docs/integration-testing.md)), then run
the `doctor` tool to confirm each one is reachable.

## MCP client registration

Rutherford is client-agnostic: every tool behaves identically from any MCP client. The command to
run is `rutherford-mcp-server` (equivalently `python -m rutherford`). Configuration uses the same
command on Windows, macOS, and Linux.

### Claude Code

```sh
claude mcp add rutherford -- rutherford-mcp-server
```

### Claude Desktop / Cursor / other JSON-config clients

Add to the client's MCP servers config (Claude Desktop: `claude_desktop_config.json`; Cursor:
`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "rutherford-mcp-server"
    }
  }
}
```

If the command is not on PATH, use an absolute path (or `python -m rutherford` with the interpreter
from the environment where Rutherford is installed).

### Codex

```sh
codex mcp add rutherford -- rutherford-mcp-server
```

### Linux under WSL

Install and run Rutherford inside the same WSL distribution as the CLIs it will orchestrate, and
register it from a client running in that distribution. When a client on the Windows host must
reach a server in WSL, invoke it through `wsl.exe`:

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "wsl.exe",
      "args": ["-e", "rutherford-mcp-server"]
    }
  }
}
```

### Self-invocation example

Register Rutherford in Claude Code as above, then ask Claude Code to use Rutherford's `delegate`
tool with `cli="claude_code"`. Rutherford spawns a fresh, isolated `claude -p` subprocess, distinct
from your session, and returns its result. The same works for Codex calling `codex`. The depth
guard (`max_depth`, default 3) bounds any self-referential chain.

See [docs/mcp-client-integration.md](docs/mcp-client-integration.md) for more clients and detail.

## Safety model

Every delegation runs in one of four safety modes, defaulting to the most restrictive:

| Mode | Meaning |
| --- | --- |
| `read_only` (default) | Inspect only. Review and consensus are read-only by nature. |
| `propose` | The agent may propose changes (e.g. a diff) but not apply them. |
| `write` | The agent may modify the workspace, subject to the CLI's approvals. |
| `yolo` | The agent may act without approval prompts (the CLI's bypass mode). |

`write` and `yolo` require an explicit argument and a trusted-workspace check -- the target
directory must be on the configured `trusted_workspaces` allowlist, or the call must pass
`trust_workspace=true`. No adapter ever defaults to its permission-bypass flag. Invocations are
always built as an argv list, never a shell string. See [docs/security.md](docs/security.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) -- the layered design and the two core interfaces.
- [docs/configuration.md](docs/configuration.md) -- config file, env overrides, generic adapters.
- [docs/adding-a-cli.md](docs/adding-a-cli.md) -- the contract and checklist for adding a CLI.
- [docs/integration-testing.md](docs/integration-testing.md) -- installing and authenticating each CLI, and running the suite.
- [docs/mcp-client-integration.md](docs/mcp-client-integration.md) -- registration for many clients.
- [docs/troubleshooting.md](docs/troubleshooting.md) -- common problems and fixes.
- [docs/security.md](docs/security.md) -- the security model in depth.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The whole core is testable without a real CLI; run
`just check` before pushing, then `just test-integration` for whatever CLIs your machine has.

## License

MIT (c) John Chapman. See [LICENSE](LICENSE).
