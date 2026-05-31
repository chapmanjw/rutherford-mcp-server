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
                 ______________
                /              \
               |   ___    ___   |
               |  / o \  / o \  |#####
               |  \___/  \___/  |#####]===   cranial implant
               |       __       |  |||
               |      |  |      |
               |       \/       |
                \    ______    /
                 \__/      \__/
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
| Antigravity | `antigravity` | `agy -p "<prompt>"` (answer read from the transcript file) | OS credential store (Google account) | 2026-05-30 |
| Kiro | `kiro` | `kiro-cli chat --no-interactive "<prompt>"` | `KIRO_API_KEY` (Pro/Pro+/Power) or `kiro-cli login` | 2026-05-30 |
| OpenCode | `opencode` | `opencode run --format json -q "<prompt>"` | provider key or `opencode auth login` | 2026-05-30 (docs) |
| Goose | `goose` | `goose run -q -t "<prompt>" --no-session` | `GOOSE_PROVIDER` + provider key | 2026-05-30 (docs) |

Antigravity's print-mode model is fixed (no model selector). Codex on Windows installs as an npm
shim, which Rutherford launches via `cmd.exe` automatically while still passing arguments as a list.
A seventh, well-behaved CLI can be added without code -- see [docs/adding-a-cli.md](docs/adding-a-cli.md).

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

## Quick start

After registering Rutherford with an MCP client (below), call its tools:

- `capabilities` -- list every CLI, whether it is installed, its auth status, and its models.
- `doctor` -- health-probe each adapter and diagnose anything unavailable.
- `delegate(cli, prompt, ...)` -- hand a task to one CLI. Defaults to `read_only`.
- `consensus(targets, prompt, ...)` -- ask several CLIs the same thing in parallel.
- `review(targets, paths|diff, ...)` and `plan(cli, goal, ...)` -- built on the two primitives.
- `job_status` / `job_result` -- poll a background job started with `mode="async"`.
- `list_roles` -- the persona presets (`planner`, `codereviewer`, `security`, `debugger`).

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
