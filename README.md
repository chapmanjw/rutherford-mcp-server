<p align="center">
  <img src="https://raw.githubusercontent.com/chapmanjw/rutherford-mcp-server/main/docs/images/logo.png" width="200" alt="Rutherford logo">
</p>

# Rutherford MCP Server

Give your AI coding agent a crew. Rutherford is a [Model Context Protocol](https://modelcontextprotocol.io)
server that lets one coding CLI delegate work to, debate with, and build consensus across a group of
others — Claude Code, Codex, Cursor, Qwen Code, Antigravity, Kiro, OpenCode, and Goose, plus
optional local models via Ollama and LM Studio. It runs them as headless subprocesses and brings
their answers back in one normalized shape. It is CLI-only: it orchestrates terminal coding agents
and never calls a model provider API directly (a local model is reached through its own command —
`ollama` or `lms` — not its HTTP API).

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

> Named for the irrepressibly cheerful engineer aboard the USS Cerritos in *Star Trek: Lower Decks*,
> who has a gift for getting heterogeneous systems to cooperate. That is the job here: one agent hands
> work to a crew of others and brings the results back. *Star Trek* and *Lower Decks* are trademarks of
> their respective owners; this is an unaffiliated, fan-named open-source project.

## Why you'd want this

You are deep in a session with one coding agent. Then you hit a moment where one opinion isn't enough:

- You're about to commit to a design and want a **second and third opinion** before you do.
- Two models disagree and you want to watch them actually **argue it out**, not just answer in parallel.
- A diff is risky and you want **several reviewers** on it, with the must-fix issues separated from nits.
- You want to **hand off a long refactor** to a different agent and keep working while it runs.
- You want a **fresh, unbiased critique** of the code you just wrote, from an instance with no memory of
  the conversation that produced it.

Rutherford does all of that from inside the agent you're already talking to. You describe what you want
in plain language; your agent translates it into Rutherford's tools. You rarely name the tools yourself.

## How it works

Rutherford is a stdio MCP server. Your MCP client (a coding CLI or a desktop app) calls it, and it
spawns the target CLIs as fresh, isolated headless subprocesses — argv arrays, never shell strings,
read-only by default, and depth-bounded so a CLI that calls itself can't recurse forever.

```
   your MCP client (Claude Code, Cursor, Codex, Claude Desktop, ...)
        |  MCP over stdio
        v
   rutherford-mcp-server
        |  fresh subprocess per call (read_only by default)
        +--> claude -p "..." --output-format json
        +--> codex exec --json
        +--> kiro-cli chat --no-interactive "..."
        +--> opencode run --format json "..."
        +--> goose run -t "..." --no-session
        +--> cursor-agent -p --output-format json
        +--> qwen -o json
        +--> ollama run <model>   (optional local model)
        +--> lms chat <model> -p "..."   (optional local model)
        +--> agy -p "..."   (answer read from the transcript file)
```

Every answer comes back in the same envelope regardless of the CLI's native output format, so your agent
compares apples to apples. A CLI that errors or isn't installed comes back as one failed voice without
sinking the rest of a panel.

## Supported CLIs

Invocation flags verified 2026-05-30. Pin your CLI versions and re-verify after upgrades; each adapter
keeps all of its CLI-specific details in one file, so a change is a one-file edit.

| CLI | Adapter id | How Rutherford runs it | Auth |
| --- | --- | --- | --- |
| Claude Code | `claude_code` | `claude -p "<prompt>" --output-format json` | subscription/OAuth login or `ANTHROPIC_API_KEY` |
| Codex | `codex` | `codex exec --json` (prompt on stdin) | ChatGPT login or `OPENAI_API_KEY` |
| Cursor | `cursor` | `cursor-agent -p --output-format json` (prompt on stdin) | `cursor-agent login` or `CURSOR_API_KEY` |
| Qwen Code | `qwen` | `qwen -o json` (prompt on stdin) | `qwen` OAuth login or `OPENAI_API_KEY` |
| Kiro | `kiro` | `kiro-cli chat --no-interactive "<prompt>"` | `KIRO_API_KEY` or `kiro-cli login` |
| OpenCode | `opencode` | `opencode run --format json -q "<prompt>"` | provider key or `opencode auth login` |
| Goose | `goose` | `goose run -q -t "<prompt>" --no-session` | `GOOSE_PROVIDER` + provider key |
| Antigravity | `antigravity` | `agy -p "<prompt>"` (answer from the transcript file) | Google account login |
| Ollama (local) | `ollama` | `ollama run <model>` (prompt on stdin) | none — local daemon |
| LM Studio (local) | `lmstudio` | `lms chat <model> -p "<prompt>"` | none — local |

Ollama and LM Studio are optional, bring-your-own local models: name a model per call with `model=`,
or set `[adapters.<id>] default_model` in your config (they have no built-in default).
`capabilities`/`doctor` mark them `optional: true`, and they stay out of an auto-`all` panel unless
you name them. Local CPU/iGPU inference is slow, so a longer `[adapters.<id>] timeout_s` and per-CLI
flags via `extra_args` are worth setting — see [docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md).

LM Studio also reaches **remote models over [LM Link](https://lmstudio.ai)**: a model loaded on
another machine on your network is addressed by its normal model key (e.g. `openai/gpt-oss-120b`) and
runs on that machine — `capabilities` lists it and `delegate`/`consensus` route to it with no extra
setup, so a single panel can span your local box and several remote machines.

An eleventh, well-behaved CLI can be added without code — see
[docs/adding-a-cli.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/adding-a-cli.md).

---

## Setup

### 1. Install Rutherford

It's a Python 3.11+ package. Install it as a tool so the `rutherford-mcp-server` command is on your PATH:

```sh
uv tool install rutherford-mcp-server
# or: pipx install rutherford-mcp-server
# or: pip install rutherford-mcp-server
```

### 2. Install and sign in to the CLIs you want to orchestrate

Rutherford does not install or authenticate the target CLIs — it drives the ones you already have. Install
whichever you want a crew of, and log in to each (subscription login or the relevant API key; see
[docs/integration-testing.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/integration-testing.md)).
You don't need all ten; two is enough for a consensus or a debate.

### 3. Register Rutherford with your MCP client

The command to run is `rutherford-mcp-server` (equivalently `python -m rutherford`). Same command on
Windows, macOS, and Linux.

**Claude Code**

```sh
claude mcp add rutherford -- rutherford-mcp-server
```

**Codex**

```sh
codex mcp add rutherford -- rutherford-mcp-server
```

**Claude Desktop / Cursor / other JSON-config clients** (Claude Desktop: `claude_desktop_config.json`;
Cursor: `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "rutherford": {
      "command": "rutherford-mcp-server"
    }
  }
}
```

If the command isn't on PATH, use an absolute path, or `python -m rutherford` with the interpreter from the
environment where Rutherford is installed. For WSL and more clients, see
[docs/mcp-client-integration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/mcp-client-integration.md).

### 4. Scaffold your config, then confirm the crew is reachable

Run the setup wizard to detect which CLIs you're signed in to and write a starter config plus a panel of
them:

```sh
rutherford-mcp-server init
```

It prints the plan and writes the files only after you confirm (it never overwrites an existing file).
Once Rutherford is registered, you can do the same conversationally — ask your agent to "set
up Rutherford" and the `setup` tool proposes the same files for you to approve. Then have your agent run
`doctor` to confirm each CLI is installed, authenticated, and reachable.

---

## Tutorials

You drive Rutherford in plain language from your MCP client. Each tutorial below is a prompt you can paste,
with a note on what Rutherford does under the hood. Everything defaults to read-only.

### See who's on the crew

> Which coding CLIs can Rutherford reach right now, and which am I signed in to? Then run doctor and tell
> me if anything's misconfigured.

`capabilities` is an instant, free snapshot of installed state, auth, and models. `doctor` goes further and
live-checks any CLI that has no status command (like Antigravity, whose auth only shows up once a real
round trip confirms it).

### Hand one task to one agent

> Use Rutherford to have Codex read `src/auth/session.py` and explain how token refresh works. Read-only.

A `delegate` to one CLI. You get back one normalized result: the answer, timing, token cost, and a session
id you can resume later. Add a model if you want a specific one ("ask Kiro with the cheap
`claude-haiku-4.5` model to ...").

### Get a second and third opinion

> I think the deadlock is in `queue.py`. Ask Claude Code, Codex, and Qwen the same question — where is it
> and how would you fix it? — and show me their answers side by side.

A `consensus` across three targets, one independent voice each, run in parallel. To poll *everyone* you're
signed in to, just don't name targets: "ask every coding agent I'm logged into whether a UUID or a ULID is
a better primary key." Rutherford builds the panel from every installed, authenticated CLI (optional local models like Ollama
and LM Studio are left out unless you name them) and tells you in `skipped` which it left out and why.

### Run a real debate

This is the one that isn't just parallel answers. In a debate, round one is each voice's independent take;
in every later round, each voice sees the others' latest positions and is asked to rebut and revise.

> Run a 3-round debate between Claude Code, Codex, and Kiro: "is UUIDv7 or ULID the better primary key for a
> high-write event table?" Show me how each position shifted, plus a closing summary.

A `debate`. The result carries the full per-round transcript, so you can retrace exactly who said what and
where someone changed their mind, followed by a closing synthesis of where the panel converged and where it
still splits. In a real run of that exact prompt, all three opened with "UUIDv7" for different reasons,
then in round two Claude Code and Codex corrected a factual error in Kiro's argument — and Kiro revised its
position in response. Optional stances ("have Cursor argue for it and Claude Code argue against") keep a
voice on an assigned side the whole way through.

### Turn a panel into a decision

When you want an answer, not a transcript, give consensus a strategy. Each voice is asked for a verdict and
Rutherford aggregates them.

> Ask claude_code, codex, and qwen "is this migration safe to ship?" and take the majority verdict, with
> each ending in a one-word VERDICT line.

A `consensus` with `strategy: majority`. You get back the `outcome`, the winning `decision`, and every
voice's verdict alongside its full reasoning. The strategies:

| Strategy | What it does |
| --- | --- |
| `all-voices` | Every voice, no aggregation (the default). |
| `unanimous` | Every eligible voice must weigh in and agree; a failed or unparseable voice vetoes. |
| `majority` | A verdict must exceed 50% of all eligible voices (failed/unparseable count in the denominator); no verdict over the bar is `no_majority`. |
| `plurality` | The single top-scoring verdict wins even below 50%; a tie at the top is `tied`. (This was the pre-1.1 `majority` behavior.) |
| `weighted` | Like `majority` but on summed target weight: one verdict must exceed 50% of total eligible weight, else `no_majority`. |
| `parity-pair` | Compares a proposer against parity counterweights; disagreement or a missing counterweight escalates. |

Verdicts are read from a final `VERDICT: <token>` line, or as JSON if you pass a `verdict_schema`. The `min_quorum` config field (default `1`) sets how many parseable voices an aggregating strategy needs; below it the outcome is `no_quorum`. An optional `judge` target (ideally a non-participant) writes the synthesis or closing instead of the first voice, recorded as `synthesis_by` in the result. The same `judge` option applies to `debate`.

### Save a crew as a panel and reuse it

Once you have a group you keep reaching for, save it as a named panel instead of listing the targets every
time.

```toon
# ~/.rutherford/panels.toon
panels:
  design-roundtable:
    description: Lineage-diverse design review
    strategy: parity-pair
    targets[3]:
      - cli: claude_code
        model: opus
        label: proposer
      - cli: codex
        label: implementer
      - cli: kiro
        model: deepseek-3.2
        label: dissenter
        parity: true
```

> Run my `design-roundtable` panel on this: "should this API return a stream or a page?"

`consensus`, `debate`, and `review` all accept `panel="design-roundtable"`. Panels live in
`~/.rutherford/panels.toon` (global) or `<project>/.rutherford/panels.toon` (project-specific, which
overrides a global panel of the same name). After editing the file, ask your agent to "reload Rutherford's
panels" and it picks up the change without a restart.

### Review a diff across several reviewers

> Review my staged diff with Claude Code and Codex as reviewers. Findings by file and line, must-fix
> separated from nits, and call out anything only one of them flagged.

A `review` — read-only, using the `codereviewer` role — over a diff or a set of paths, across one or more
targets. Point it at paths instead ("review everything under `src/payments/` for injection bugs") and the
reviewers read the files themselves.

### Get an implementation plan

> Use Rutherford's planner on Claude Code to turn "add OAuth2 device-code login to the CLI" into an ordered,
> step-by-step plan, with the files each step touches and the risky parts flagged.

A `plan` — one target, the `planner` role, read-only. The bundled roles are `planner`, `codereviewer`,
`security`, and `debugger`; ask "what roles does Rutherford have?" to list them (each shows its source). Add
your own as markdown or TOON files under `~/.rutherford/roles/` (or a project's `.rutherford/roles/`); a
project role overrides a same-named global one. See
[docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md).

### Let an agent actually make the change

> Let Codex apply the fix in `C:\work\myrepo` — write mode, you have my permission to edit files there. Add
> the missing null check and a test that covers it.

A `delegate` in `write` mode. Write and yolo are never the default: they require both an explicit mode and a
trusted workspace (an allowlisted path or your per-call go-ahead), so an agent can't modify a directory by
accident. See the safety model below.

### Kick off a long job and keep working

> Start a big refactor on OpenCode in the background — "convert the data layer to the repository pattern" in
> `C:\work\myrepo` — and just give me the job id.

`delegate` (or `consensus` / `debate`) in async mode returns a job id immediately. Use `list_jobs` to see all retained jobs, `job_status` / `job_result` to poll a specific one, and `cancel_job` to cancel a running or pending job.

### Get a fresh, unbiased take on your own work

> Spin up a separate Claude Code instance through Rutherford — one with no memory of this conversation — to
> critique the design we just wrote.

Rutherford can target the very CLI you're talking to. It spawns a fresh, isolated subprocess that is
distinct from your session and can't reach back into it. A depth guard (`max_depth`, default 3) keeps a
CLI-calls-itself chain bounded.

---

## Safety model

Every delegation runs in one of four safety modes, defaulting to the most restrictive:

| Mode | Meaning |
| --- | --- |
| `read_only` (default) | Inspect only. Review, consensus, debate, and plan are read-only by nature. |
| `propose` | The agent may propose changes (e.g. a diff) but not apply them. |
| `write` | The agent may modify the workspace, subject to the CLI's own approvals. |
| `yolo` | The agent may act without approval prompts (the CLI's bypass mode). |

`write` and `yolo` require an explicit argument and a trusted-workspace check: the target directory must be
on the configured `trusted_workspaces` allowlist, or the call must pass `trust_workspace=true`. No adapter
ever defaults to its permission-bypass flag, and invocations are always built as an argv list, never a
shell string. See [docs/security.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/security.md).

## Configuration

The main config is a small TOML file (`config.toml` in your platform config dir, or a project-local
`rutherford.toml`); panels and custom roles live in their own files under `~/.rutherford/` and a project's
`.rutherford/`. The full reference — every field, the discovery and precedence rules, the panel and role
file formats, and config-defined generic adapters — is in
[docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md).

## Documentation

- [docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md) — config file, panels, custom roles, strategies, generic adapters.
- [docs/architecture.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/architecture.md) — the layered design and the two core interfaces.
- [docs/adding-a-cli.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/adding-a-cli.md) — the contract and checklist for adding a CLI.
- [docs/mcp-client-integration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/mcp-client-integration.md) — registration for many clients.
- [docs/integration-testing.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/integration-testing.md) — installing and authenticating each CLI.
- [docs/troubleshooting.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/troubleshooting.md) — common problems and fixes.
- [docs/security.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/security.md) — the security model in depth.

## Experimental status

Rutherford drives independent third-party CLIs. Their headless flags, output formats, and auth mechanisms
change between releases, and a CLI update can change or remove something an adapter relies on. Every flag in
this repo was verified against the CLI's own `--help` and docs on the date noted above. Pin your CLI
versions, re-verify after upgrades, and treat the integration as evolving.

## Contributing

See [CONTRIBUTING.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/CONTRIBUTING.md). The whole
core is testable without a real CLI; run `just check` before pushing, then `just test-integration` for
whatever CLIs your machine has installed and authenticated.

## License

MIT (c) John Chapman. See [LICENSE](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/LICENSE).
