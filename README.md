<!-- mcp-name: io.github.chapmanjw/rutherford -->

<p align="center">
  <img src="https://raw.githubusercontent.com/chapmanjw/rutherford-mcp-server/main/docs/images/logo.png" width="180" alt="Rutherford logo">
</p>

<h1 align="center">Rutherford</h1>

<p align="center"><b>Give your AI coding agent a crew.</b></p>

<p align="center">
A CLI-only MCP server that drives the coding agents you already run — Claude Code, Codex, Cursor,<br>
and seven more — to delegate work, debate, and reach consensus. It reuses each CLI's own login and<br>
never calls a model provider's hosted API, so there are no new keys to manage.
</p>

<p align="center">
  <a href="https://pypi.org/project/rutherford-mcp-server/"><img src="https://img.shields.io/pypi/v/rutherford-mcp-server" alt="PyPI version"></a>
  <img src="https://img.shields.io/pypi/pyversions/rutherford-mcp-server" alt="Python 3.11+">
  <a href="https://github.com/chapmanjw/rutherford-mcp-server/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/rutherford-mcp-server" alt="MIT license"></a>
  <a href="https://github.com/chapmanjw/rutherford-mcp-server/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/chapmanjw/rutherford-mcp-server/ci.yml" alt="CI"></a>
</p>

<p align="center">
  <a href="cursor://anysphere.cursor-deeplink/mcp/install?name=rutherford&amp;config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJydXRoZXJmb3JkLW1jcC1zZXJ2ZXIiXX0="><img src="https://img.shields.io/badge/Add_to-Cursor-000000?logo=cursor&logoColor=white" alt="Add to Cursor"></a>
  <a href="vscode:mcp/install?%7b%22name%22%3a%22rutherford%22%2c%22command%22%3a%22uvx%22%2c%22args%22%3a%5b%22rutherford-mcp-server%22%5d%7d"><img src="https://img.shields.io/badge/Install-VS_Code-007ACC?logo=visualstudiocode&logoColor=white" alt="Install in VS Code"></a>
  <a href="vscode-insiders:mcp/install?%7b%22name%22%3a%22rutherford%22%2c%22command%22%3a%22uvx%22%2c%22args%22%3a%5b%22rutherford-mcp-server%22%5d%7d"><img src="https://img.shields.io/badge/Install-VS_Code_Insiders-24bfa5?logo=visualstudiocode&logoColor=white" alt="Install in VS Code Insiders"></a>
</p>

<p align="center"><i>Drives:</i>
Claude Code · Codex · Cursor · Qwen Code · Kiro · OpenCode · Goose · Droid · Mistral Vibe · GitHub Copilot · Antigravity · Ollama · LM Studio</p>

```sh
uv tool install rutherford-mcp-server
```

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#the-tools">Tools</a> ·
  <a href="#recipes">Recipes</a> ·
  <a href="#safety-model">Safety</a> ·
  <a href="#troubleshooting">Troubleshooting</a> ·
  <a href="#documentation">Docs</a>
</p>

Rutherford is a [Model Context Protocol](https://modelcontextprotocol.io) server. Your MCP client (a
coding CLI or a desktop app) calls it, and it spawns the target CLIs as fresh, isolated headless
subprocesses — argv arrays, never shell strings, read-only by default. Every answer comes back in one
normalized shape, so your agent compares like with like. You drive it in plain language; your agent
translates your words into Rutherford's tools, and you rarely name the tools yourself.

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

## See it work

The mode that isn't just parallel answers is `debate`. Round one is each voice's independent take; in
every later round, each voice sees the others' latest positions and is asked to rebut and revise. Here
is one real run of a three-round debate across Claude Code, Codex, and Kiro, condensed to the moment
that matters:

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
         kiro          revises its position: cross-node, UUIDv7's timestamp prefix gives
                       the locality without relying on a per-process assumption

result   converged on UUIDv7, with a closing synthesis of where the panel agreed and why
```

In round two, two models corrected a factual error in the third's argument, and that third model
changed its mind. The call returns the full per-round transcript plus the closing synthesis, so you
can retrace exactly who said what and where someone revised. This is one run — debates do not always
converge or change a mind — but when they do, the transcript shows precisely where.

## Why you'd want this

You are deep in a session with one coding agent, and you hit a moment where one opinion isn't enough:

- You're about to commit to a design and want a second and third opinion before you do.
- Two models disagree and you want to watch them argue it out, not just answer in parallel.
- A diff is risky and you want several reviewers on it, with the must-fix issues separated from nits.
- You want to hand off a long refactor to a different agent and keep working while it runs.
- You want a fresh critique of the code you just wrote, from an instance with no memory of the
  conversation that produced it.

Most multi-model tools ask you to wire up provider API keys and pay per token; Rutherford reuses the CLI
logins you already have.

## Quickstart

You bring the crew. The prerequisite that surprises people: Rutherford does not install or authenticate
any coding CLI — it drives the ones you already have. You need Python 3.11+ and at least two target CLIs
installed and signed in (two is enough for a consensus or a debate). If you already use Claude Code or
Codex, you have most of what you need.

**1. Install Rutherford.**

```sh
uv tool install rutherford-mcp-server
# or: pipx install rutherford-mcp-server  /  pip install rutherford-mcp-server
```

**2. Register it with your client.** One-click for Cursor and VS Code with the badges at the top of this
page, or by hand:

```sh
claude mcp add rutherford -- rutherford-mcp-server      # Claude Code
codex mcp add rutherford -- rutherford-mcp-server       # Codex
```

For Claude Desktop, Cursor, and other JSON-config clients:

```json
{ "mcpServers": { "rutherford": { "command": "rutherford-mcp-server" } } }
```

If `rutherford-mcp-server` isn't on your PATH, use an absolute path or `python -m rutherford` with the
interpreter from the environment where you installed it. WSL and more clients:
[docs/mcp-client-integration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/mcp-client-integration.md).

**3. Scaffold a config.**

```sh
rutherford-mcp-server init
```

`init` detects which CLIs you're signed in to, prints the plan, and writes a starter `config.toml` plus a
panel only after you confirm (it never overwrites an existing file). You can do the same conversationally
once Rutherford is registered: ask your agent to "set up Rutherford."

**4. Run `doctor` first.** Multi-CLI auth and PATH is the most common thing that goes wrong, so confirm
the crew is reachable before your first real task:

> Run Rutherford's doctor and tell me which CLIs are installed, authenticated, and reachable.

```
doctor
  claude_code   ok          authenticated   models: opus, sonnet, haiku
  codex         ok          authenticated
  qwen          ok          auth: unknown   (verified live — a round trip succeeded)
  cursor        not-found   install it, then re-run doctor
  kiro          needs-login run `kiro-cli login` or set KIRO_API_KEY
```

Green on two or more CLIs means you're ready. Any other line tells you exactly what to fix.

**No paid CLI subscription?** Run your first consensus for free against local models. Install
[Ollama](https://ollama.com), pull one model, and Rutherford will use it — no key, no account:

```sh
ollama pull llama3.2
```

> Ask the `ollama` model and any other CLI I'm signed into the same question — "UUID or ULID for a
> primary key?" — and show me their answers side by side.

## The tools

You rarely call these by name; your agent picks them from your request. Everything defaults to read-only.

| Tool | When to reach for it |
| --- | --- |
| [`delegate`](#recipes) | Hand one task to one CLI; get one normalized result back. |
| [`consensus`](#recipes) | Ask several CLIs the same thing in parallel; optionally aggregate to a verdict. |
| [`debate`](#recipes) | Have several CLIs argue across rounds and return the full transcript. |
| [`review`](#recipes) | Multi-reviewer, read-only code review of a diff or a set of paths. |
| [`plan`](#recipes) | Ask one CLI for an ordered implementation plan. |
| `capabilities` / `doctor` | `capabilities` is an instant snapshot of install, auth, and models; `doctor` live-checks the CLIs whose auth only shows on a real round trip. |

Long tasks run as background jobs (`mode=async`): the call returns a job id immediately, and `list_jobs`,
`job_status`, `job_result`, and `cancel_job` manage them. `setup`, `list_roles`, and `reload_panels`
round out the surface.

## How it works

```
   your MCP client (Claude Code, Cursor, Codex, Claude Desktop, ...)
        |  MCP over stdio
        v
   rutherford-mcp-server
        |  fresh subprocess per call (read_only by default, argv arrays never shell strings)
        +--> claude -p "..." --output-format json
        +--> codex exec --json
        +--> cursor-agent -p --output-format json
        +--> ... seven more, each behind one adapter file
```

A CLI that errors or isn't installed comes back as one failed voice without sinking the rest of a panel.
Parallel-agent runners point N agents at N tasks and let you read the diffs; Rutherford points several
agents at one task and reconciles their answers into one shape.

## Recipes

<details>
<summary><b>Paste-able prompts for each tool</b></summary>

These are how-to recipes — paste the prompt to your agent, which translates it into a tool call. The
longer ones, plus saved panels and the strategy walkthrough, live in
[docs/recipes.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/recipes.md).

**Hand one task to one agent**

> Use Rutherford to have Codex read `src/auth/session.py` and explain how token refresh works. Read-only.

A `delegate` to one CLI. You get back the answer, timing, token cost, and a session id you can resume.

**Get a second and third opinion**

> I think the deadlock is in `queue.py`. Ask Claude Code, Codex, and Qwen the same question — where is it
> and how would you fix it? — and show me their answers side by side.

A `consensus` across three targets in parallel. To poll everyone you're signed into, don't name targets.

**Run a debate**

> Run a 3-round debate between Claude Code, Codex, and Kiro on whether UUIDv7 or ULID is the better
> primary key for a high-write event table. Show how each position shifted, plus a closing summary.

Round one is each voice's independent take; later rounds feed each voice the others' positions to rebut
and revise. The result carries the full per-round transcript and a closing synthesis.

**Review a diff across several reviewers**

> Review my staged diff with Claude Code and Codex as reviewers. Findings by file and line, must-fix
> separated from nits, and call out anything only one of them flagged.

A `review` — read-only, using the `codereviewer` role. Point it at paths instead and the reviewers read
the files themselves.

**Get an implementation plan**

> Use Rutherford's planner on Claude Code to turn "add OAuth2 device-code login to the CLI" into an
> ordered, step-by-step plan, with the files each step touches and the risky parts flagged.

A `plan` — one target, the `planner` role, read-only.

**Kick off a long job and keep working**

> Start a big refactor on OpenCode in the background — convert the data layer to the repository pattern
> in `C:\work\myrepo` — and just give me the job id.

Async mode returns a job id immediately. Ask "is that Rutherford job done yet?" to poll it.

</details>

## Safety model

Every delegation runs in one of four modes, defaulting to the most restrictive.

| Mode | Meaning |
| --- | --- |
| `read_only` (default) | Inspect only. `review` and `plan` are clamped to this mode. |
| `propose` | May propose changes (a diff) but not apply them. |
| `write` | May modify the workspace, subject to the CLI's own approvals. |
| `yolo` | May act without approval prompts (the CLI's bypass mode). |

A call that omits `safety_mode` adopts the configured `default_safety_mode` (`read_only` out of the box);
an explicit value always wins. `write` and `yolo` — explicit or configured — require a trusted workspace:
the target directory must be on the `trusted_workspaces` allowlist, or the call must pass
`trust_workspace=true`. No adapter ever defaults to its permission-bypass flag, and invocations are
always built as an argv list, never a shell string. A depth guard (`max_depth`, default 3) keeps a
CLI-calls-itself chain bounded. Full detail:
[docs/security.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/security.md).

## Supported CLIs

<details>
<summary><b>The full adapter matrix and the versions each release is confirmed against</b></summary>

Each adapter keeps all of its CLI-specific details in one file, so a change is a one-file edit. Adding a
CLI is one small code adapter that reuses the shared parsing toolkit — see
[docs/adding-a-cli.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/adding-a-cli.md).

| CLI | Adapter id | How Rutherford runs it | Auth |
| --- | --- | --- | --- |
| Claude Code | `claude_code` | `claude -p "<prompt>" --output-format json` | subscription/OAuth or `ANTHROPIC_API_KEY` |
| Codex | `codex` | `codex exec --json` (prompt on stdin) | ChatGPT login or `OPENAI_API_KEY` |
| Cursor | `cursor` | `cursor-agent -p --output-format json` | `cursor-agent login` or `CURSOR_API_KEY` |
| Qwen Code | `qwen` | `qwen -o json` (prompt on stdin) | `qwen` OAuth or `OPENAI_API_KEY` |
| Kiro | `kiro` | `kiro-cli chat --no-interactive "<prompt>"` | `KIRO_API_KEY` or `kiro-cli login` |
| OpenCode | `opencode` | `opencode run --format json -q "<prompt>"` | provider key or `opencode auth login` |
| Goose | `goose` | `goose run -q -t "<prompt>" --no-session` | `GOOSE_PROVIDER` + provider key |
| Droid (Factory) | `droid` | `droid exec --output-format json` (prompt on stdin) | `FACTORY_API_KEY`/`FACTORY_TOKEN` or `droid` login |
| Mistral Vibe | `vibe` | `vibe --output json --trust --agent <mode> -p "<prompt>"` | `MISTRAL_API_KEY` or `vibe --setup` |
| GitHub Copilot CLI | `copilot` | `copilot -p "<prompt>" --output-format json` | GitHub PAT (Copilot Requests scope) or `copilot` login |
| Antigravity | `antigravity` | `agy -p "<prompt>"` (answer from the transcript file) | Google login |
| Ollama (local) | `ollama` | `ollama run <model>` (prompt on stdin) | none — local daemon |
| LM Studio (local) | `lmstudio` | `lms chat <model> -p "<prompt>"` | none — local |

**Confirmed CLI versions.** Rutherford's own code is production-stable; its CLI integrations target
third-party tools whose headless flags and output formats change between releases. Each Rutherford
release records the CLI versions it was last verified against. Re-check after a CLI upgrade, and pin if
you can.

| CLI | Confirmed with Rutherford 1.4.0 | Check yours |
| --- | --- | --- |
| Claude Code | 2.1.172 | `claude --version` |
| Codex | 0.135.0 | `codex --version` |
| Cursor | 2026.05.28 | `cursor-agent --version` |
| Qwen Code | 0.17.0 | `qwen --version` |
| Kiro | 2.6.1 | `kiro-cli --version` |
| OpenCode | 1.15.13 | `opencode --version` |
| Goose | 1.36.0 | `goose --version` |
| Droid (Factory) | 0.144.2 | `droid --version` |
| Mistral Vibe | 2.14.1 | `vibe --version` |
| GitHub Copilot CLI | 1.0.61 | `copilot --version` |
| Antigravity | 1.0.7 | `agy --version` |
| Ollama | 0.30.6 | `ollama --version` |
| LM Studio (`lms`) | build efce996 | `lms version` |

Ollama and LM Studio are optional, bring-your-own local models: name a model per call with `model=`, or
set `[adapters.<id>] default_model`. `capabilities`/`doctor` mark them `optional: true`, and they stay
out of an auto-`all` panel unless you name them. Local CPU/iGPU inference is slow, so a longer
`[adapters.<id>] timeout_s` is worth setting. LM Studio also reaches remote models over
[LM Link](https://lmstudio.ai): a model loaded on another machine on your network is addressed by its
normal model key and runs on that machine, reached through `lms` rather than any vendor API.

</details>

## Strategies and saved panels

<details>
<summary><b>Turn a panel into a verdict, and reuse a crew by name</b></summary>

Give `consensus` a strategy and each voice is asked for a verdict, which Rutherford aggregates. Verdicts
are read from a final `VERDICT: <token>` line, or as JSON if you pass a `verdict_schema`.

| Strategy | What it does |
| --- | --- |
| `all-voices` | Every voice, no aggregation (the default). |
| `unanimous` | Every eligible voice must weigh in and agree; a failed or unparseable voice vetoes. |
| `majority` | A verdict must exceed 50% of all eligible voices (failed/unparseable count in the denominator). |
| `plurality` | The single top-scoring verdict wins even below 50%; a tie at the top is `tied`. |
| `weighted` | Like `majority` but on summed target weight. |
| `parity-pair` | Compares a proposer against parity counterweights; disagreement escalates. |

The `min_quorum` field (default 1) sets how many parseable voices an aggregating strategy needs. An
optional `judge` target (ideally a non-participant) writes the synthesis, recorded as `synthesis_by`;
the same option applies to `debate`. Full mechanics:
[docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md).

Save a crew you keep reaching for as a named panel:

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

`consensus`, `debate`, and `review` all accept `panel="design-roundtable"`. Panels live in
`~/.rutherford/panels.toon` (global) or `<project>/.rutherford/panels.toon` (project-specific, which
overrides a global panel of the same name). After editing the file, ask your agent to "reload Rutherford's
panels" and it picks up the change without a restart.

</details>

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| A CLI shows as `not-found` | It isn't on Rutherford's PATH; install it and re-run `doctor`. |
| A CLI shows as `needs-login` | Sign in with that CLI's own login, or set its API key; Rutherford never logs in for you. |
| `WORKSPACE_NOT_TRUSTED` | The target dir isn't on `trusted_workspaces`; add it, or pass `trust_workspace=true`. |
| `MAX_DEPTH_EXCEEDED` | A CLI-calls-itself chain hit `max_depth` (default 3); raise it or shorten the chain. |
| A local model times out | Raise `[adapters.<id>] timeout_s`; a cold model load can exceed the global budget. |

More in [docs/troubleshooting.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/troubleshooting.md).

## Configuration

The main config is a small TOML file (`config.toml` in your platform config dir, or a project-local
`rutherford.toml`); panels and custom roles live in their own files under `~/.rutherford/` and a
project's `.rutherford/`. The bundled roles are `planner`, `codereviewer`, `security`, and `debugger`;
add your own as markdown or TOON under `~/.rutherford/roles/`. Full reference:
[docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md).

## Status

Rutherford's orchestration core — the safety model, the normalized envelope, the aggregation strategies —
is production-stable and covered by a strict test gate. Its CLI integrations are version-sensitive: they
drive independent third-party CLIs whose flags, output formats, and auth mechanisms change between
releases, and a CLI update can break something an adapter relies on. The versions each release was
verified against are listed under [Supported CLIs](#supported-clis). Pin your CLI versions where you can,
and re-verify after upgrades.

## Documentation

- [docs/configuration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/configuration.md) — config file, panels, custom roles, strategies.
- [docs/recipes.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/recipes.md) — the full cookbook of paste-able prompts.
- [docs/architecture.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/architecture.md) — the layered design and the two core interfaces.
- [docs/adding-a-cli.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/adding-a-cli.md) — the contract and checklist for adding a CLI.
- [docs/mcp-client-integration.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/mcp-client-integration.md) — registration for many clients.
- [docs/integration-testing.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/integration-testing.md) — installing and authenticating each CLI.
- [docs/security.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/security.md) — the security model in depth.
- [docs/troubleshooting.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/docs/troubleshooting.md) — common problems and fixes.

## Contributing

See [CONTRIBUTING.md](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/CONTRIBUTING.md). The
whole core is testable without a real CLI; run `just check` before pushing, then `just test-integration`
for whatever CLIs your machine has installed and authenticated.

## License

MIT (c) John Chapman. See [LICENSE](https://github.com/chapmanjw/rutherford-mcp-server/blob/main/LICENSE).
