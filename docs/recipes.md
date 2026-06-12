# Recipes

Task-oriented how-to guides for driving Rutherford from your MCP client. Each recipe is a prompt you
paste to your agent, which translates it into a Rutherford tool call, plus a note on what happens under
the hood. Everything defaults to read-only; the write-mode recipes call out the trust check explicitly.

Find the recipe that matches your problem and start there — you don't need to read top to bottom. For the
short list of the most common ones, see the [Recipes section of the README](../README.md#recipes).

## See who's on the crew

> Which coding CLIs can Rutherford reach right now, and which am I signed in to? Then run doctor and tell
> me if anything's misconfigured.

`capabilities` is an instant, free snapshot of installed state, auth, and models. `doctor` goes further
and live-checks any CLI that has no status command of its own (like Antigravity, whose auth only shows up
once a real round trip confirms it). Run `doctor` first whenever a setup feels off — multi-CLI auth and
PATH is the most common thing that goes wrong.

## Hand one task to one agent

> Use Rutherford to have Codex read `src/auth/session.py` and explain how token refresh works. Read-only.

A `delegate` to one CLI. You get back one normalized result: the answer, timing, token cost, and a
session id you can resume later. Name a model if you want a specific one:

> Ask Kiro with the cheap `claude-haiku-4.5` model to summarize what `src/payments/refund.py` does.

## Get a second and third opinion

> I think the deadlock is in `queue.py`. Ask Claude Code, Codex, and Qwen the same question — where is it
> and how would you fix it? — and show me their answers side by side.

A `consensus` across three targets, one independent voice each, run in parallel. To poll *everyone*
you're signed into, just don't name targets:

> Ask every coding agent I'm logged into whether a UUID or a ULID is a better primary key.

Rutherford builds the panel from every installed, authenticated CLI (optional local models like Ollama
and LM Studio are left out unless you name them) and tells you in `skipped` which it left out and why.

## Run a real debate

In a debate, round one is each voice's independent take; in every later round, each voice sees the
others' latest positions and is asked to rebut and revise.

> Run a 3-round debate between Claude Code, Codex, and Kiro: "is UUIDv7 or ULID the better primary key for
> a high-write event table?" Show me how each position shifted, plus a closing summary.

A `debate`. The result carries the full per-round transcript, so you can retrace exactly who said what
and where someone changed their mind, followed by a closing synthesis of where the panel converged and
where it still splits. In a real run of that exact prompt, all three opened with UUIDv7 for different
reasons; then in round two Claude Code and Codex corrected a factual error in Kiro's argument, and Kiro
revised its position in response.

Optional stances keep a voice on an assigned side the whole way through:

> Have Cursor argue for it and Claude Code argue against, three rounds, then summarize.

## Turn a panel into a decision

When you want an answer, not a transcript, give consensus a strategy. Each voice is asked for a verdict
and Rutherford aggregates them.

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

Verdicts are read from a final `VERDICT: <token>` line, or as JSON if you pass a `verdict_schema`. The
`min_quorum` config field (default 1) sets how many parseable voices an aggregating strategy needs; below
it the outcome is `no_quorum`. An optional `judge` target (ideally a non-participant) writes the synthesis
or closing instead of the first voice, recorded as `synthesis_by` in the result. The same `judge` option
applies to `debate`.

## Save a crew as a panel and reuse it

Once you have a group you keep reaching for, save it as a named panel instead of listing the targets
every time.

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

## Review a diff across several reviewers

> Review my staged diff with Claude Code and Codex as reviewers. Findings by file and line, must-fix
> separated from nits, and call out anything only one of them flagged.

A `review` — read-only, using the `codereviewer` role — over a diff or a set of paths, across one or more
targets. Point it at paths instead and the reviewers read the files themselves:

> Review everything under `src/payments/` for injection bugs with Claude Code and Qwen.

## Get an implementation plan

> Use Rutherford's planner on Claude Code to turn "add OAuth2 device-code login to the CLI" into an
> ordered, step-by-step plan, with the files each step touches and the risky parts flagged.

A `plan` — one target, the `planner` role, read-only. The bundled roles are `planner`, `codereviewer`,
`security`, and `debugger`; ask "what roles does Rutherford have?" to list them (each shows its source).
Add your own as markdown or TOON files under `~/.rutherford/roles/` (or a project's `.rutherford/roles/`);
a project role overrides a same-named global one. See
[configuration.md](configuration.md).

## Let an agent actually make the change

> Let Codex apply the fix in `C:\work\myrepo` — write mode, you have my permission to edit files there.
> Add the missing null check and a test that covers it.

A `delegate` in `write` mode. Write and yolo are never the default: they require both an explicit mode and
a trusted workspace (an allowlisted path or your per-call go-ahead), so an agent can't modify a directory
by accident. See the [safety model](../README.md#safety-model).

## Kick off a long job and keep working

> Start a big refactor on OpenCode in the background — "convert the data layer to the repository pattern"
> in `C:\work\myrepo` — and just give me the job id.

`delegate` (or `consensus` / `debate`) in async mode returns a job id immediately. Use `list_jobs` to see
all retained jobs, `job_status` / `job_result` to poll a specific one, and `cancel_job` to cancel a
running or pending job.

> List my Rutherford jobs. Is that refactor done? If it is, show me the result.

## Get a fresh, unbiased take on your own work

> Spin up a separate Claude Code instance through Rutherford — one with no memory of this conversation —
> to critique the design we just wrote.

Rutherford can target the very CLI you're talking to. It spawns a fresh, isolated subprocess that is
distinct from your session and can't reach back into it, so the critique comes from an instance with no
memory of the conversation that produced the work. A depth guard (`max_depth`, default 3) keeps a
CLI-calls-itself chain bounded.
