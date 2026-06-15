<!-- mcp-name: io.github.chapmanjw/rutherford -->

<p align="center">
  <img src="https://raw.githubusercontent.com/chapmanjw/rutherford-mcp-server/main/docs/images/logo.png" width="180" alt="Rutherford logo">
</p>

<h1 align="center">Rutherford</h1>

<p align="center"><b>Give your AI coding agent a crew.</b></p>

<p align="center">
A stdio MCP server that orchestrates the coding agents you already run — Claude Code, Codex, Cursor,<br>
Goose, and more — over the <a href="https://agentclientprotocol.com">Agent Client Protocol (ACP)</a>.
Hand work to one agent, ask several in parallel,<br>or have them argue it out. It reuses each agent's
own login and never calls a model provider's API.
</p>

<p align="center">
  <a href="https://pepy.tech/projects/rutherford-mcp-server"><img src="https://static.pepy.tech/personalized-badge/rutherford-mcp-server?period=total&units=NONE&left_color=GREY&right_color=ORANGE&left_text=downloads" alt="PyPI Downloads"></a>
  <a href="https://pypi.org/project/rutherford-mcp-server/"><img src="https://img.shields.io/pypi/v/rutherford-mcp-server" alt="PyPI version"></a>
  <img src="https://img.shields.io/pypi/pyversions/rutherford-mcp-server" alt="Python 3.11+">
  <a href="https://github.com/chapmanjw/rutherford-mcp-server/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/rutherford-mcp-server" alt="MIT license"></a>
  <a href="https://github.com/chapmanjw/rutherford-mcp-server/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/chapmanjw/rutherford-mcp-server/ci.yml" alt="CI"></a>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#the-tools">Tools</a> ·
  <a href="#the-agent-roster">Agents</a> ·
  <a href="#safety-modes">Safety</a> ·
  <a href="#documentation">Docs</a>
</p>

```sh
uv tool install rutherford-mcp-server
```

## What Rutherford is

Rutherford is a [Model Context Protocol](https://modelcontextprotocol.io) server that speaks the
[Agent Client Protocol](https://agentclientprotocol.com) on the other side. Your MCP client (a coding
CLI or a desktop app) calls Rutherford's tools; Rutherford spawns each target coding agent as an ACP
server over stdio and drives it through a real `initialize` / `new_session` / `prompt` exchange.

It is the ACP *client*, and each coding agent is an ACP *agent*. That distinction matters: under ACP
the protocol negotiates the answer, token usage, tool activity, and permissions as structured events,
so Rutherford never scrapes an agent's stdout and never reimplements a CLI's features. It also never
calls a model provider's API directly — every answer comes from an agent you already log into, in the
agent's own account.

```
   your MCP client (Claude Code, Cursor, Codex, Claude Desktop, ...)
        |  MCP over stdio
        v
   rutherford-mcp-server          (the ACP client)
        |  ACP over stdio, one session per voice
        +--> goose acp
        +--> codex-acp                 (the Zed adapter fronting Codex)
        +--> claude-agent-acp          (the Zed adapter fronting Claude Code)
        +--> ... 16 more built-in agents, all config-driven
```

A voice that fails to spawn, handshake, or answer comes back as one failed result in a structured
envelope, never an aborted panel.

## See it work

The mode that is not just parallel answers is `debate`. Round one is each voice's independent take; in
every later round, each voice sees the others' latest positions and is asked to rebut and revise. Each
voice keeps one persistent ACP session across the rounds, so it remembers its own prior reasoning and
only the delta is sent each round — the capability the old subprocess-per-call model could not offer.

```
prompt   "Is UUIDv7 or ULID the better primary key for a high-write event table?"
panel    claude_code, codex, kiro    rounds: 3

round 1  claude_code   UUIDv7 — the timestamp prefix gives B-tree index locality
         codex         UUIDv7 — standardized and DB-native; monotonic within a process
         kiro          UUIDv7 — but argues ULID is BOTH lexicographically sortable AND
                       collision-resistant across concurrent writers

round 2  claude_code   flags that Kiro conflates two properties: ULID's sortability and
                       its per-process monotonicity are not the same guarantee
         codex         agrees — the monotonic guarantee is per-process, not cross-node
         kiro          revises: cross-node, UUIDv7's timestamp prefix gives the locality
                       without relying on a per-process assumption

result   converged on UUIDv7, with a closing synthesis of where the panel agreed and why
```

The call returns the full per-round transcript plus the closing synthesis, so you can retrace who said
what and where someone revised. Debates do not always converge or change a mind, but when they do the
transcript shows precisely where.

## Quickstart

You bring the crew. Rutherford does not install or authenticate any coding agent — it drives the ones
you already have. You need Python 3.11+ and at least two ACP-capable agents installed and signed in
(two is enough for a consensus or a debate). If you already use Claude Code or Codex, you have most of
what you need.

**1. Install Rutherford.**

```sh
uv tool install rutherford-mcp-server
# or: pipx install rutherford-mcp-server  /  pip install rutherford-mcp-server
```

This puts the console entry point `rutherford-mcp-server` on your PATH. The same command starts the
stdio server on Windows, macOS, and Linux; `python -m rutherford` is equivalent.

**2. Register it with your MCP client.**

```sh
claude mcp add rutherford -- rutherford-mcp-server      # Claude Code
codex mcp add rutherford -- rutherford-mcp-server       # Codex
```

For Claude Desktop, Cursor, and other JSON-config clients:

```json
{ "mcpServers": { "rutherford": { "command": "rutherford-mcp-server" } } }
```

If `rutherford-mcp-server` is not on the client's PATH, use an absolute path, or `python -m rutherford`
with the interpreter from the environment where you installed it. More clients and WSL:
[docs/mcp-client-integration.md](docs/mcp-client-integration.md).

**3. Scaffold a config (optional).** Rutherford works with zero config. To write a starter file, either
run the one-shot CLI from your terminal:

```sh
rutherford-mcp-server init          # or: python -m rutherford init  [--global] [--yes]
```

or, once it is registered with a client, ask for the `setup` tool:

> Run Rutherford's setup and write a project config.

Both resolve the config path, write a commented starter `config.toml` at the effective defaults, and never
clobber an existing file. `init` targets `<cwd>/.rutherford/config.toml` (or the global path with
`--global`); `setup` returns the path and content to the client and writes with `write=true`.

**4. Run `doctor` first.** Multi-agent auth and PATH is the most common thing that goes wrong, so
confirm the crew actually drives before your first real task:

> Run Rutherford's doctor and tell me which agents spawn, handshake, and answer.

`doctor` probes each agent with a real read-only ACP round trip — the only trustworthy health signal,
since there is no cheap non-interactive auth check. Each report is `ok`, `no_answer`,
`handshake_failed`, `not_installed`, or `error`. Two or more `ok` agents means you are ready.

**No paid agent subscription?** Run your first consensus for free against a local model. With
[Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai) running, Rutherford auto-detects
each tool-capable model and registers it as a `goose`-based agent — no key, no account. See
[docs/local-models.md](docs/local-models.md).

## The tools

You rarely call these by name; your agent picks them from your request. Everything defaults to
read-only.

| Tool | What it does |
| --- | --- |
| `delegate` | Hand one task to one ACP agent; get one normalized result back. |
| `consensus` | Ask the same prompt of several agents in parallel; return every voice. |
| `debate` | Have several agents argue across rounds (persistent sessions) and return the full transcript. |
| `review` | Review a diff or a working dir's changes across one or more agents — a code-review-shaped consensus. |
| `plan` | Produce an implementation plan for a task without making changes (read-only by construction). |
| `continue_job` | Resume or build on a completed durable job (delegate / consensus / debate) with a new prompt. |
| `analyze` | Run an offline report over the kept run corpus (e.g. `historical_agreement` cross-lineage agreement). |
| `capabilities` | List the registered agents (id, display name, launch command, provider) — the cheap snapshot. |
| `doctor` | Probe each agent with a real read-only ACP round trip and report conformance. |
| `discover` | Detect installed ACP agents from the community registry and propose reviewable config blocks. |
| `list_roles` | List the role personas you can pass as `role="<id>"`. |
| `setup` | Show where config lives and scaffold a starter `config.toml`; the first-run helper. |
| `reload_panels` | Reload the named multi-agent panel definitions from config without restarting the server. |
| `list_jobs` | List the background jobs being tracked (every status), newest first. |
| `activity` | Show only the jobs in flight right now, each with a live elapsed time. |
| `job_status` | Report one background job's status and timings. |
| `job_result` | Return a finished job's result envelope (identical to the sync envelope). |
| `cancel_job` | Cancel a running background job and tear down its work. |

Shared arguments on `delegate` / `consensus` / `debate`: `working_dir`, `files` (paths to put in
scope), `safety_mode`, `timeout_s`, `role`, and `mode` (`sync` or `async`). `delegate` also takes
`trust_workspace` for the mutating modes; `debate` takes `rounds`, `judge`, and `synthesize`.

## The agent roster

Rutherford ships **19 built-in agents** with curated launch commands and quirks (the Windows npm-shim
resolution, per-agent handshake budgets, a fixed provider) that a bare `acp.json` cannot express, so
they work with zero config:

| id | agent | how it launches | login |
| --- | --- | --- | --- |
| `goose` | Goose | `goose acp` | provider key / `goose configure` |
| `opencode` | OpenCode | `opencode acp` | a configured provider |
| `vibe` | Mistral Vibe | `vibe-acp` | Mistral / `vibe` login |
| `cline` | Cline | `cline --acp` | Cline's own service auth |
| `junie` | Junie | `junie --acp=true` | JetBrains login |
| `kimi` | Kimi Code | `kimi acp` | Moonshot login |
| `openhands` | OpenHands | `openhands acp` | a configured provider |
| `codex` | Codex | `codex-acp` | the existing Codex (ChatGPT) login — no API key |
| `claude_code` | Claude Code | `claude-agent-acp` | the existing Claude Code login — no API key |
| `copilot` | GitHub Copilot | `copilot --acp` | GitHub Copilot plan |
| `qwen` | Qwen Code | `qwen --acp` | Qwen OAuth / OpenAI-compatible key |
| `droid` | Factory Droid | `droid exec --output-format acp` | Factory login |
| `cursor` | Cursor | `cursor-agent acp` | Cursor subscription |
| `kiro` | Kiro | `kiro-cli acp` | Kiro login / `KIRO_API_KEY` |
| `pi` | Pi | `pi-acp` | Pi login |
| `hermes` | Hermes | `hermes acp` | Nous endpoint |
| `gemini` | Gemini CLI | `gemini --acp` | Google / Gemini CLI login |
| `qoder` | Qoder | `qodercli --acp` | Qoder login |
| `grok` | Grok | `grok agent stdio` | xAI login + SuperGrok subscription |

`codex` and `claude_code` launch through the official Zed adapters (`codex-acp` and
`claude-agent-acp`, npm `@agentclientprotocol/*`), which front the Codex and Claude Code CLIs as ACP
servers and reuse the existing CLI login — no API key. `cline` drives over ACP only with Cline's own
service auth (a ChatGPT-subscription or OpenRouter provider set in the desktop app does not reach the
headless `--acp` path). `hermes` depends on the configured Nous model and its latency can be high.
`gemini` is Google's official Gemini CLI (the `--acp` mode works as of CLI 0.46.0). `qoder`'s `--acp`
flag is real but hidden from `--help`, and Qoder's installer drops `qodercli` at `~/.qoder/bin/` rather
than on PATH — add that directory to PATH, point `[agents.qoder] command` at the full path, or let
`discover` find it. `grok` (xAI) is ACP-native and connects cleanly, but a completed turn needs a SuperGrok
subscription — without it the model call returns `403`; run `doctor connect_only=true` to confirm Rutherford
can reach and configure it (it reports `reachable` and the advertised models) independent of the
entitlement. Not every agent drives cleanly on every machine — run `doctor` to see which actually answer
here.

**Config-driven agents.** Under ACP an agent is just how to launch it plus a few quirks, so the roster
is config-driven. An `[agents.<id>]` section overrides a built-in's command / env / provider / model,
disables one with `enabled = false`, or defines a brand-new agent (any unknown id, which must supply a
launch `command`). `enabled_agents` restricts the registry to an allowlist. The launch fields mirror
the Zed/Cline `acp.json` shape, and the loader auto-imports an `acp.json` beside the global config or
in the project's `.rutherford/`. See [docs/adding-an-agent.md](docs/adding-an-agent.md).

**Local models.** With `auto_detect_local_models` on (the default), Rutherford probes a running Ollama
(`:11434`) and LM Studio (`:1234`) at startup and registers each tool-capable model as a `goose`-based
ACP agent automatically. You can also point an agent at a local runtime by hand. See
[docs/local-models.md](docs/local-models.md).

## Safety modes

Every delegation runs in one of four modes, defaulting to the most restrictive. Rutherford is the
permission authority at the moment of each ACP tool call: it answers the agent's filesystem-write,
terminal-execution, and tool-permission requests according to the mode.

| Mode | Meaning |
| --- | --- |
| `read_only` (default) | Inspect only. Reads are served; writes, terminal execution, and tool-permission requests are denied. |
| `propose` | Same denials as `read_only` — the agent may describe changes but not apply them. |
| `write` | The agent may modify the workspace, subject to the agent's own approvals. |
| `yolo` | The agent may act without approval prompts. |

A call that omits `safety_mode` adopts the configured `default_safety_mode` (`read_only` out of the
box); an explicit value always wins. `write` and `yolo` require a trusted workspace: the target
`working_dir` must be on the `trusted_workspaces` allowlist, or the call must pass
`trust_workspace=true`. Full detail: [docs/security.md](docs/security.md).

## Jobs, roles, and config

**Background jobs.** Pass `mode="async"` to `delegate` / `consensus` / `debate` to run the work off
the request path: the call returns a small `{job_id, status, tool}` envelope immediately, and the work
runs as an in-memory task. Manage it with `list_jobs`, `activity`, `job_status`, `job_result`, and
`cancel_job`. A finished job's result envelope is byte-for-byte the same as the sync path's. Jobs are
in-memory and clear on restart.

**Roles.** A role is a reusable system prompt. Pass `role="<id>"` to `delegate` / `consensus` /
`debate` and the persona is prepended to your prompt. Five built-ins ship as package data:
`principal-reviewer`, `architect`, `debugger`, `security-reviewer`, and `explainer`. A `role_dirs`
directory adds new roles or overrides a built-in. `list_roles` enumerates the catalog.

**Config.** Rutherford works with zero config. When you do configure it, a TOML file at the global or
project scope sets the agent roster, defaults, and safety policy; `RUTHERFORD_*` environment variables
override specific fields. Full reference: [docs/configuration.md](docs/configuration.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) — the v3 ACP-native architecture and the key seams.
- [docs/configuration.md](docs/configuration.md) — the complete `RutherfordConfig` reference and config discovery.
- [docs/adding-an-agent.md](docs/adding-an-agent.md) — config-driven agents, `acp.json` import, local backends.
- [docs/local-models.md](docs/local-models.md) — Ollama and LM Studio as first-class voices.
- [docs/recipes.md](docs/recipes.md) — task-oriented usage recipes.
- [docs/mcp-client-integration.md](docs/mcp-client-integration.md) — wiring Rutherford into MCP clients.
- [docs/security.md](docs/security.md) — the safety model and the permission engine in depth.
- [docs/troubleshooting.md](docs/troubleshooting.md) — common problems and fixes.
- [docs/integration-testing.md](docs/integration-testing.md) — running the real-agent integration suite.

## The name

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

> Named for the cheerful engineer aboard the USS Cerritos in *Star Trek: Lower Decks*, who has a gift
> for getting heterogeneous systems to cooperate. That is the job here: one agent hands work to a crew
> of others and brings the results back. *Star Trek* and *Lower Decks* are trademarks of their
> respective owners; this is an unaffiliated, fan-named open-source project.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The whole core is testable without a real agent; run
`just check` before pushing, then `just test-integration` for whatever agents your machine has
installed and authenticated.

## License

MIT (c) John Chapman. See [LICENSE](LICENSE).
