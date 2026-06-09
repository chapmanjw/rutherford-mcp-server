# MCP client integration

Rutherford is a plain stdio MCP server with no knowledge of which client started it, so it behaves
identically from any client. The command to run is `rutherford-mcp-server` (equivalently
`python -m rutherford`). Install it first so the command is on PATH:

```sh
uv tool install rutherford-mcp-server   # or pipx install / pip install
```

If the command is not on PATH for your client, use an absolute path, or `python -m rutherford` with
the interpreter from the environment where Rutherford is installed. The same command works on
Windows, macOS, and Linux.

## Claude Code

```sh
claude mcp add rutherford -- rutherford-mcp-server
```

Then, in a session, the Rutherford tools (`delegate`, `consensus`, `debate`, `review`, `plan`,
`capabilities`, `doctor`, `job_status`, `job_result`, `list_jobs`, `cancel_job`, `list_roles`,
`reload_panels`, `setup`) are available.

## Claude Desktop

Edit `claude_desktop_config.json` (Settings -> Developer -> Edit Config):

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

Or add it to `~/.codex/config.toml` under the MCP servers section, with `command =
"rutherford-mcp-server"`.

## Any other MCP client

Configure a stdio server whose command is `rutherford-mcp-server`. No arguments or environment are
required beyond the API keys / sessions of the CLIs you want to orchestrate (see
[integration-testing.md](integration-testing.md)).

## Passing configuration and credentials

Rutherford reads its own settings from a config file and `RUTHERFORD_*` environment variables (see
[configuration.md](configuration.md)), and it reads each target CLI's credentials from that CLI's
own environment variables or stored session. If your client lets you set environment variables for
an MCP server, you can pass `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and so on there; otherwise set
them in the environment from which the client launches the server.

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "rutherford-mcp-server",
      "env": {
        "RUTHERFORD_DEFAULT_TIMEOUT_S": "300",
        "ANTHROPIC_API_KEY": "..."
      }
    }
  }
}
```

Do not commit a config that contains real keys.

## Windows and WSL

On native Windows, the JSON snippets above work as-is once `rutherford-mcp-server` is on PATH.

To orchestrate CLIs that live inside WSL, install and run Rutherford inside the same WSL
distribution as those CLIs, and register it from a client running in that distribution. When a
client on the Windows host must reach a Rutherford installed in WSL, launch it through `wsl.exe`:

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

Rutherford detects WSL and translates Windows and WSL paths when an adapter's runtime differs from
the host, so a working directory passed from a Windows client is converted for a Linux CLI.

## Self-invocation

Self-invocation is explicitly supported: a client may register Rutherford and then delegate back to
that same CLI. For example, from Claude Code call `delegate` with `cli="claude_code"` -- Rutherford
spawns a fresh, isolated `claude -p` subprocess that is independent of your session and cannot reach
back into it. Codex calling `codex` works the same way. Rutherford tracks a delegation depth and
propagates it through `RUTHERFORD_DEPTH`; it refuses to spawn beyond `max_depth` (default 3) and
caps the number of targets per call, so a CLI-calls-itself chain stays bounded. Do not set
`RUTHERFORD_DEPTH` yourself.
