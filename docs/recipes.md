# Recipes

Task-oriented how-to guides for driving Rutherford from your MCP client. Each recipe is a prompt you
paste to your agent, which translates it into a Rutherford tool call, plus a note on what happens under
the hood. Everything defaults to read-only; the write-mode recipe calls out the trust check explicitly.

Find the recipe that matches your problem and start there. You do not need to read top to bottom.

## The minimal call

Every tool below takes a small set of required arguments and sensible defaults for the rest — you never
need to send a fully-populated payload. The bare minimum for each:

| tool | required arguments | example |
| --- | --- | --- |
| `delegate` | `cli`, `prompt` | `{ "cli": "codex", "prompt": "explain src/auth/session.py" }` |
| `consensus` | `prompt` (omit `targets`, or pass `"all"`, to fan out to every agent) | `{ "prompt": "where is the deadlock in queue.py?" }` |
| `debate` | `prompt`, `targets` (at least two) | `{ "prompt": "UUIDv7 or ULID?", "targets": ["codex", "claude_code"] }` |
| `doctor` | none | `{}` |

`debate` is the one tool that needs an explicit `targets` (a debate has no meaningful single-agent form), so
send the two arguments above rather than an empty object. Everything else — safety mode, models, effort,
persistence, timeouts — has a default; add an argument only to override it.

## See who is on the crew

> Which coding agents can Rutherford reach right now? Then run doctor and tell me which actually drive.

`capabilities` is an instant snapshot of the registered agents: id, display name, launch command,
provider, configured `default_model` / `fallback_model`, how Rutherford selects models
(`launch_argv` vs `in_session`), and whether `effort` has a known knob. It does not spawn agents.
`doctor(connect_only=true)` is the handshake-only probe that returns each agent's live advertised
model ids; full `doctor` goes further with a real read-only ACP turn — the only trustworthy health
signal — and reports `ok`, `no_answer`, `handshake_failed`, `not_installed`, or `error`. Run
`doctor` first whenever a setup feels off; multi-agent auth and PATH is the most common thing that
goes wrong.

## Hand one task to one agent

> Use Rutherford to have Codex read `src/auth/session.py` and explain how token refresh works. Read-only.

A `delegate` to one agent. You get back one normalized result: the answer, timing, token cost where the
agent reports it, and the ACP session id. Name a model if you want a specific one:

> Ask Goose with `qwen3:8b` to summarize what `src/payments/refund.py` does.

Put files in scope with the `files` argument, or just name them in the prompt:

> Have Claude Code review `src/db/pool.py` and `src/db/conn.py` for connection leaks. Read-only.

## Get a second and third opinion

> I think the deadlock is in `queue.py`. Ask Claude Code, Codex, and Qwen the same question — where is it
> and how would you fix it? — and show me their answers side by side.

A `consensus` across three targets, each an independent voice running in its own ACP session in
parallel. The result is every voice — one failing voice comes back as a failed result, never an aborted
panel. Targets can be `cli` strings, `cli:model` strings, or `{cli, model}` objects.

## Run a real debate

In a debate, round one is each voice's independent take; in every later round, each voice sees the
others' latest positions and is asked to rebut and revise. Each voice keeps one persistent ACP session
across the rounds, so it remembers its own prior reasoning and only the delta is sent — the capability
the old subprocess model could not offer.

> Run a 3-round debate between Claude Code, Codex, and Kiro: "is UUIDv7 or ULID the better primary key for
> a high-write event table?" Show me how each position shifted, plus a closing summary.

A `debate`. The result carries the full per-round transcript, so you can retrace who said what and
where someone changed their mind, followed by a closing synthesis (on by default) of where the panel
converged. A debate needs at least two targets and runs up to `rounds` rounds (`max_debate_rounds`
caps it).

Steer a voice's side with a stance, or name a neutral judge for the synthesis:

> Have Cursor argue for it and Claude Code argue against, two rounds, and let Codex write the closing
> summary as the judge.

## Apply a persona with a role

> Review my changes to `src/payments/` as a principal engineer would: must-fix separated from nits,
> across Claude Code and Codex. Read-only.

Pass `role="principal-reviewer"` and the persona is prepended to your prompt for every voice. The five
built-in roles:

| role | persona |
| --- | --- |
| `principal-reviewer` | a rigorous senior reviewer who separates must-fix from nits |
| `architect` | a system designer who weighs tradeoffs and names the failure modes |
| `debugger` | a root-cause debugger who proposes the smallest correct fix |
| `security-reviewer` | a threat-modeling reviewer who rates findings by severity |
| `explainer` | a clear teacher who explains code from the reader's understanding |

> What roles does Rutherford have?

`list_roles` returns the catalog. Add your own as markdown files under a `role_dirs` directory; a file
whose id matches a built-in overrides it. See [configuration.md](configuration.md#roles).

## Use a local model

> Ask my local `qwen3:8b` and Claude Code the same question — "UUID or ULID for a primary key?" — and
> show me both answers.

With Ollama or LM Studio running and a tool-capable model loaded, Rutherford auto-detects it and
registers it as a `goose`-based agent (`ollama-qwen3-8b`, `lmstudio-...`). No key, no account. Pin a
specific model or a remote host with an `[agents.<id>]` entry. See [local-models.md](local-models.md).

## Let an agent actually make the change

> Let Codex apply the fix in `C:\work\myrepo` — write mode, you have my permission to edit files there.
> Add the missing null check and a test that covers it.

A `delegate` in `write` mode. Write and yolo are never the default: they require both an explicit mode
and a trusted workspace (an allowlisted path or a per-call `trust_workspace=true`), so an agent cannot
modify a directory by accident. Rutherford answers the agent's filesystem-write and tool-permission
requests according to the mode. See the [safety model](../README.md#safety-modes).

## Kick off a long job and keep working

> Start a big refactor on OpenCode in the background — "convert the data layer to the repository pattern"
> in `C:\work\myrepo` — and just give me the job id.

`delegate` / `consensus` / `debate` in `mode="async"` returns a `{job_id, status, tool}` envelope
immediately. The work runs as an in-memory task; its eventual result envelope is byte-for-byte the same
as the sync path's.

- `list_jobs` — every retained job, newest first.
- `activity` — only the jobs in flight right now, each with a live elapsed time, longest-running first.
- `job_status` — one job's status and timings.
- `job_result` — a finished job's result envelope.
- `cancel_job` — cancel a running job and tear down its work.

> List my Rutherford jobs. Is that refactor done? If it is, show me the result.

Jobs are in-memory and clear on restart, and a finished one is evicted after `job_ttl_s` — collect the
result before then.

## Get a fresh, unbiased take on your own work

> Spin up a separate Claude Code instance through Rutherford — one with no memory of this conversation —
> to critique the design we just wrote, as an architect.

Rutherford can target the very agent you are talking to. It opens a fresh, isolated ACP session that is
distinct from your conversation and cannot reach back into it, so the critique comes from an instance
with no memory of the work that produced it. A depth guard (`max_depth`, default 3) keeps a
calls-itself chain bounded.
