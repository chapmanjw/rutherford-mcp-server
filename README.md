<p align="center">
  <img src="https://raw.githubusercontent.com/chapmanjw/rutherford-mcp-server/main/docs/images/logo.png" width="200" alt="Rutherford logo">
</p>

# Rutherford MCP Server - Multi-Agent Consensus, Debates, Reviews, and Delegation

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets one AI coding
CLI delegate work to, and build consensus across, a crew of others. Rutherford runs other agentic
coding CLIs (Claude Code, Codex, Cursor, Qwen Code, Antigravity, Kiro, OpenCode, Goose) as headless
subprocesses and returns each one's answer in a single normalized envelope. It is CLI-only: it
orchestrates terminal coding agents and never calls a model provider API directly.

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
        +--> cursor-agent -p --output-format json   (prompt on stdin)
        +--> qwen -o json                           (prompt on stdin)
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
| Cursor | `cursor` | `cursor-agent -p --output-format json --trust` (prompt on stdin) | `cursor-agent login` or `CURSOR_API_KEY` | 2026-05-30 |
| Qwen Code | `qwen` | `qwen -o json` (prompt on stdin) | `qwen` OAuth login or `OPENAI_API_KEY` | 2026-05-30 |

Antigravity's print-mode model is fixed (no model selector). Cursor on a free plan can only use the
`auto` model; named models need a paid plan. Both Cursor and Qwen install as Windows shims, which
Rutherford launches via `cmd.exe` while feeding the prompt on stdin. Codex on Windows installs as an npm
shim, which Rutherford launches via `cmd.exe` automatically while still passing arguments as a list.
A seventh, well-behaved CLI can be added without code -- see [docs/adding-a-cli.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/adding-a-cli.md).

## Using Rutherford

You drive Rutherford from your MCP client in plain language. You describe what you want, and your
agent translates it into Rutherford's tools (`delegate`, `consensus`, `debate`, `review`, `plan`,
`capabilities`, `doctor`, `job_status`, `job_result`, `list_roles`, `reload_panels`) -- you rarely name the tools or
their arguments yourself. You name a CLI (`claude_code`, `codex`, `cursor`, `qwen`, `kiro`,
`opencode`, `goose`, `antigravity`), optionally a model, and what you want done. Everything defaults
to read-only.

The examples below are prompts you can paste, grouped by what you are trying to do. Each is followed
by a note on what Rutherford does under the hood.

### See which agents are available

> Which coding CLIs can Rutherford reach right now, and which ones am I signed in to?

> Run Rutherford's doctor and tell me if anything's misconfigured before I start delegating.

The first runs `capabilities` (an instant, free snapshot of installed state, auth, and models). The
second runs `doctor`, which also live-checks any CLI that has no status command -- like Antigravity,
whose auth only shows up once a real round trip confirms it.

### Hand one task to a specific agent

> Use Rutherford to have Codex read `src/auth/session.py` and explain how token refresh works.
> Read-only -- don't change anything.

> Ask Kiro with the cheap `claude-haiku-4.5` model to summarize what changed in this 1,500-line log
> and list the three most likely root causes.

A single `delegate` to one CLI (and model), read-only. You get back one normalized result -- the
answer text, timing, token cost, and a session id you can resume.

### Get a second and third opinion

> I think the deadlock is in `queue.py`. Ask Claude Code, Codex, and Qwen the same question --
> "where is the deadlock and how would you fix it?" -- and show me their answers side by side.

A `consensus` across three targets, one independent voice each. A CLI that errors or isn't installed
comes back as a single failed voice without sinking the rest of the panel.

### Poll every CLI you have authenticated

> Ask every coding agent I'm logged into the same question -- "is a UUID or a ULID a better primary
> key for a high-write table?" -- and show me all their answers.

A `consensus` with no targets named (or `targets: "all"`): Rutherford builds the panel from every
adapter it finds installed and authenticated, each at its default model, and tells you in `skipped`
which it left out and why (not installed, needs login). If one voice asked for a model its plan
doesn't allow, that voice retries once on the CLI's default model rather than dropping out.

### Run a multi-round debate

> Use Rutherford to run a 3-round debate on this claim: "We should replace our internal REST APIs
> with gRPC." Put Cursor (model `auto`), Claude Code, and Codex on the panel, have Cursor argue for
> it and Claude Code argue against, and show me how the positions shifted plus a closing summary.

A `debate` across the named targets. Round one is each voice's independent answer; in every later
round each voice sees the others' latest positions and rebuts or revises its own, so the panel
actually argues rather than answering in isolation. The result carries the full per-round
transcript -- you can retrace exactly how each voice moved -- and an optional closing summary of
where they converged and where they still split. Optional stances (for / against / neutral) keep a
voice on its assigned side the whole way through. For a single-shot version where each agent
answers once with an assigned stance and nobody rebuts, use `consensus` with `stances` instead.

### Save a panel and reuse it

> Run my `design-roundtable` panel on this question: "should this API return a stream or a page?"

Once you have a crew you keep reaching for, save it as a named panel in `~/.rutherford/panels.toon`
(or `<project>/.rutherford/panels.toon` for a project-specific one) and name it with `panel=`
instead of listing the targets every time. A project's panel of the same name overrides your global
one, and `consensus`, `debate`, and `review` all accept `panel`. After editing the file, "reload
Rutherford's panels" picks up the change without restarting. See
[docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md) for the file format.

### Build one combined recommendation

> Ask claude_code, codex, and opencode (`openai/gpt-5.4`) for the best caching strategy for an
> append-heavy event log, then have Rutherford merge their answers into a single recommendation that
> flags where they disagree.

A `consensus` with server-side synthesis enabled: you get every voice plus one merged answer.
(Synthesis is off by default -- usually you let your own agent compare the voices.)

### Review code across several reviewers

> Review my staged diff with Rutherford using Claude Code and Codex as reviewers. Findings by file
> and line, must-fix separated from nits, and call out anything only one of them flagged.

> Have Codex and Qwen review everything under `src/payments/` for security and injection bugs.

`review` -- read-only, using the `codereviewer` role -- over a diff or a set of paths, across one or
more targets.

### Get an implementation plan

> Use Rutherford's planner on Claude Code to turn "add OAuth2 device-code login to the CLI" into an
> ordered, step-by-step plan, with the files each step touches and the risky parts flagged.

`plan` -- one target, the `planner` role, read-only. The bundled roles are `planner`, `codereviewer`,
`security`, and `debugger`; ask "what roles does Rutherford have?" to list them (each shows its
source). Add your own as markdown or TOON files under `~/.rutherford/roles/` or a project's
`.rutherford/roles/`, with a project role overriding a same-named global one (see
[docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md)).

### Let an agent actually make the change

> Let Codex apply the fix in `C:\work\myrepo` -- write mode, you have my permission to edit files in
> that folder. Add the missing null check and a test that covers it.

A `delegate` in `write` mode. Write and yolo are never the default: they require both an explicit
mode and a trusted workspace (an allowlisted path or your per-call go-ahead), so an agent can't
modify a directory by accident. See the safety model below.

### Kick off a long job and keep working

> Start a big refactor on OpenCode in the background -- "convert the data layer to the repository
> pattern" in `C:\work\myrepo` -- and just give me the job id so I can keep working.

> Is that Rutherford job done yet? Show me the result if it finished.

The first runs `delegate` in async mode and returns a job id immediately; the second polls
`job_status` / `job_result`.

### Continue where an agent left off

> Pick the review session Claude Code just ran back up, and tell it to also check the error handling
> now.

A `delegate` that passes the `session_id` from the earlier result back in, resuming that CLI's own
conversation (on the CLIs that support resume).

### Get a fresh, unbiased take (self-invocation)

> Spin up a separate Claude Code instance through Rutherford -- one with no memory of this
> conversation -- to critique the design we just wrote, so I get an outside opinion.

Rutherford can target the very CLI you're talking to: it spawns a fresh, isolated subprocess that is
distinct from your session and can't reach back into it. A depth guard (`max_depth`, default 3) and a
per-call target cap keep a CLI-calls-itself chain bounded.

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
you want to orchestrate (see [docs/integration-testing.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/integration-testing.md)), then run
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

See [docs/mcp-client-integration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/mcp-client-integration.md) for more clients and detail.

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
always built as an argv list, never a shell string. See [docs/security.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/security.md).

## Documentation

- [docs/architecture.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/architecture.md) -- the layered design and the two core interfaces.
- [docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md) -- config file, env overrides, generic adapters.
- [docs/adding-a-cli.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/adding-a-cli.md) -- the contract and checklist for adding a CLI.
- [docs/integration-testing.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/integration-testing.md) -- installing and authenticating each CLI, and running the suite.
- [docs/mcp-client-integration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/mcp-client-integration.md) -- registration for many clients.
- [docs/troubleshooting.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/troubleshooting.md) -- common problems and fixes.
- [docs/security.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/security.md) -- the security model in depth.

## Contributing

See [CONTRIBUTING.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/CONTRIBUTING.md). The whole core is testable without a real CLI; run
`just check` before pushing, then `just test-integration` for whatever CLIs your machine has.

## License

MIT (c) John Chapman. See [LICENSE](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/LICENSE).
