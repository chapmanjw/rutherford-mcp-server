# MCP client integration

Rutherford is a plain stdio MCP server with no knowledge of which client started it, so it behaves
identically from any client. The command to run is `rutherford-mcp-server` (equivalently
`python -m rutherford`). Install it first so the command is on PATH:

```sh
uv tool install rutherford-mcp-server   # or pipx install / pip install
```

If the command is not on PATH for your client, use an absolute path, or `python -m rutherford` with the
interpreter from the environment where Rutherford is installed. The same command works on Windows,
macOS, and Linux. Python 3.11+ is required.

## Claude Code

```sh
claude mcp add rutherford -- rutherford-mcp-server
```

In a session, the Rutherford tools become available: `delegate`, `consensus`, `debate`, `review`,
`plan`, `continue_job`, `analyze`, `capabilities`, `doctor`, `discover`, `list_roles`, `setup`,
`reload_panels`, `list_jobs`, `activity`, `job_status`, `job_result`, and `cancel_job` (see the
README tools table for what each does).

## Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "rutherford-mcp-server"
    }
  }
}
```

Config file locations:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

## Cursor

Edit `.cursor/mcp.json` in the project (or the global `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "rutherford-mcp-server"
    }
  }
}
```

## Codex

```sh
codex mcp add rutherford -- rutherford-mcp-server
```

Or add it to `~/.codex/config.toml` under the MCP servers section, with
`command = "rutherford-mcp-server"`.

## Any other MCP client

Configure a stdio server whose command is `rutherford-mcp-server`. No arguments are required. The
target coding agents reach their models with their own logins; sign in to each one's own flow before
starting the server (see [integration-testing.md](integration-testing.md)).

## Passing configuration and environment

Rutherford reads its own settings from a config file and `RUTHERFORD_*` environment variables (see
[configuration.md](configuration.md)). Each target agent reads its credentials from its own environment
or stored session. If your client lets you set environment variables for an MCP server, you can pass
`RUTHERFORD_*` settings (and an agent's API key, if it uses one) there; otherwise set them in the
environment from which the client launches the server.

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "rutherford-mcp-server",
      "env": {
        "RUTHERFORD_DEFAULT_TIMEOUT_S": "300"
      }
    }
  }
}
```

Do not commit a config that contains real keys.

## Windows and WSL

On native Windows the JSON snippets above work as-is once `rutherford-mcp-server` is on PATH.
Rutherford resolves npm-shim launch commands to clean stdio automatically, so an agent installed via
npm (with a `.cmd` / `.ps1` shim) drives over ACP without extra setup.

To orchestrate agents that live inside WSL, install and run Rutherford inside the same WSL distribution
as those agents, and register it from a client running in that distribution. When a client on the
Windows host must reach a Rutherford installed in WSL, launch it through `wsl.exe`:

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

Rutherford launches agents natively and does not translate paths between Windows and WSL forms, so keep
Rutherford in the same environment as the agents it orchestrates and pass working directories in that
environment's form (`/mnt/c/...` inside WSL, `C:\...` on the Windows host).

## Self-invocation

Self-invocation is supported: a client may register Rutherford and then delegate back to the same
agent. From Claude Code, call `delegate` with `cli="claude_code"` — Rutherford opens a fresh, isolated
ACP session (through `claude-agent-acp`) that is independent of your session and cannot reach back into
it. Codex calling `codex` works the same way. A `max_depth` guard (default 3) bounds a calls-itself
chain and the per-call `max_targets` bounds a panel.
