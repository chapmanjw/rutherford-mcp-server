# Rutherford configuration reference

Rutherford uses TOML for configuration. The loader discovers, merges, and validates config at
startup. A missing file is not an error; defaults apply. Invalid config (parse failure or
schema violation) raises `ConfigError` and the process exits non-zero before serving any
requests.

## Discovery and precedence

When `RUTHERFORD_CONFIG` is set, that single file is used and discovery is skipped. If the
file does not exist, startup fails immediately.

Without `RUTHERFORD_CONFIG`, the loader reads two files and merges them:

```
global config file     (lowest priority)
        +
project-local file     (overlays global; project wins on any key present in both)
        +
RUTHERFORD_* env vars  (highest priority; override specific scalar fields)
```

Nested dicts merge recursively. List and scalar values replace entirely (no append
semantics).

### Global config path

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\rutherford\config.toml` |
| Linux / macOS | `$XDG_CONFIG_HOME/rutherford/config.toml` (fallback: `~/.config/rutherford/config.toml`) |

### Project-local config

The loader searches the current working directory for `rutherford.toml`, then
`.rutherford.toml`. The first match wins; both are not read.

### Selecting a single file

```
RUTHERFORD_CONFIG=/path/to/my-config.toml
```

This disables global and project-local discovery entirely.

---

## `RutherfordConfig` fields

All fields are optional. Unset fields take the listed default.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled_adapters` | `list[str]` or omitted | all built-ins | Restrict the registry to these adapter ids. Unknown ids in this list are a startup error. |
| `adapters` | `dict[str, AdapterConfig]` | `{}` | Per-adapter overrides keyed by adapter id. |
| `default_safety_mode` | `string` | `"read_only"` | Safety posture applied when a caller omits the field. One of `read_only`, `propose`, `write`, `yolo`. |
| `default_timeout_s` | `float` | `300.0` | Per-run timeout in seconds (must be > 0). |
| `role_dirs` | `list[str]` | `[]` | Extra directories to search for custom role files (in addition to the well-known scopes; see Custom roles). Built-in roles always load. |
| `max_depth` | `int` | `3` | Maximum delegation depth (1–10). Delegations at this depth are refused. |
| `max_targets` | `int` | `8` | Maximum targets per consensus or debate call (1–32). |
| `max_debate_rounds` | `int` | `4` | Maximum rounds a single `debate` call may run (1–10). Each round is a full panel pass. |
| `min_quorum` | `int` | `1` | Minimum parseable voices (with an extracted verdict) an aggregating strategy needs before it will certify an outcome. Below it the outcome is `no_quorum`. Guards against certifying a result off one surviving voice when the rest failed. |
| `min_distinct` | `int` | `2` | Distinct-identity floor for the `low_diversity` flag on a consensus/debate result. When at least two answering voices resolve but they collapse to fewer than this many distinct models *or* distinct providers (vendors), the panel is flagged as less independent than its CLI count implied. Raise it to demand wider diversity. |
| `max_concurrency` | `int` | `max_targets` | Global cap on concurrent CLI subprocesses across all panels. Defaults to `max_targets` when not set explicitly. Overrideable via `RUTHERFORD_MAX_CONCURRENCY`. |
| `cooldown_threshold` | `int` | `3` | How many *unhealthy* failures (rate-limit, auth, timeout, spawn, output drift — not a hard-task non-zero exit) an adapter may have within `cooldown_window_s` before it is benched. A benched adapter is left out of an `expand_all` panel and skipped as a fallback candidate, but an explicit delegation to it still runs. `0` disables cooldown. In-memory, per-adapter, resets on restart. |
| `cooldown_window_s` | `float` | `120.0` | The sliding window over which `cooldown_threshold` failures are counted. |
| `cooldown_duration_s` | `float` | `60.0` | How long a benched adapter stays benched before it is tried again. |
| `trusted_workspaces` | `list[str]` | `[]` | Absolute paths under which `write` and `yolo` delegations are permitted. |
| `synthesize_default` | `bool` | `false` | Whether consensus synthesizes server-side by default. |
| `verify_read_only` | `bool` | `false` | Opt-in: after a successful `read_only` or `propose` delegation whose working directory is a git repo, fail the result with `READONLY_VIOLATED` if the tree changed. Off by default (adds git calls per delegation). |
| `probe_cache_ttl_s` | `float` | `10.0` | Seconds to cache an adapter's metadata probe results (`detect` / `check_auth` / `available_models`). Set to `0` to disable. Prevents redundant subprocess forks when `capabilities` / `doctor` / `expand_all` run in quick succession. |
| `probe_timeout_s` | `float` | `20.0` | Hard ceiling (seconds, ≥ 1) on a single metadata probe. A hung probe cannot stall `capabilities` / `doctor` / `expand_all` beyond this limit. |
| `job_ttl_s` | `float` | `3600.0` | Seconds a finished background job is retained before eviction (≥ 1). |
| `max_jobs` | `int` | `100` | Maximum background jobs retained at once (≥ 1). Creating a job past this cap (after evicting expired jobs) fails with `TOO_MANY_JOBS`. |
| `log_level` | `string` | `"info"` | Structured-log verbosity. One of `debug`, `info`, `warning`, `error`. Logs go to stderr as JSON. |
| `log_format` | `string` | `"json"` | Structured-log format. `json` emits one JSON object per line to stderr; `off` silences all logging. |

Saved panels and custom roles are not fields of this file; they live in their own files (see
Saved panels below) so the main config stays small.

### `AdapterConfig` fields (under `[adapters.<id>]`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `true` | Set to `false` to remove this adapter from the registry at startup. |
| `default_model` | `string` or omitted | none | Model passed when the caller names none. Required to use a local adapter (Ollama or LM Studio) without a per-call `model=`, since neither has a built-in default. |
| `timeout_s` | `float` or omitted | none | Per-adapter run timeout in seconds. Overrides the global `default_timeout_s` for this adapter when a call names no `timeout_s`. Useful for a slow local model whose cold load can exceed the global budget. |
| `extra_args` | `list[str]` | `[]` | Extra CLI arguments appended verbatim to the adapter's invocation. Honored by the local-model adapters Ollama (e.g. `["--keepalive", "30s"]`) and LM Studio (e.g. `["--ttl", "3600"]`); the cloud-CLI adapters ignore it. |

---

## Environment overrides

These override specific fields after the config files are merged. They do not replace the
entire config.

| Variable | Type | Overrides |
|----------|------|-----------|
| `RUTHERFORD_CONFIG` | path | Replaces file discovery; must point to an existing file. |
| `RUTHERFORD_CONFIG_DIR` | path | A panels/roles config directory searched ahead of the project and home locations (see Saved panels). |
| `RUTHERFORD_MAX_DEPTH` | integer | `max_depth` |
| `RUTHERFORD_MAX_TARGETS` | integer | `max_targets` |
| `RUTHERFORD_MAX_CONCURRENCY` | integer | `max_concurrency` |
| `RUTHERFORD_DEFAULT_TIMEOUT_S` | float | `default_timeout_s` |
| `RUTHERFORD_DEFAULT_SAFETY` | string | `default_safety_mode` |
| `RUTHERFORD_TRUSTED_WORKSPACES` | `os.pathsep`-delimited paths | `trusted_workspaces` |
| `RUTHERFORD_ROLE_DIRS` | `os.pathsep`-delimited paths | `role_dirs` |
| `RUTHERFORD_DEPTH` | integer | Set automatically on child processes to track delegation depth. Do not set this yourself. |

---

## Complete example

```toml
# rutherford.toml

# Restrict to two adapters instead of loading all built-ins.
enabled_adapters = ["claude_code", "codex"]

default_safety_mode = "read_only"
default_timeout_s   = 120.0
max_depth           = 2
max_targets         = 4
synthesize_default  = false

# Paths under which write/yolo are allowed.
trusted_workspaces = [
    "/home/user/projects/myapp",
    "/tmp/sandbox",
]

# Extra directories to search for role markdown files.
role_dirs = ["/home/user/.config/rutherford/roles"]

# Per-adapter overrides.
[adapters.claude_code]
default_model = "claude-sonnet-4-6"

[adapters.codex]
enabled = false   # disable without removing from enabled_adapters

# A local Ollama model. It has no built-in default, so set one here (or pass model= per call).
# Sampling (temperature, num_ctx) lives in the model's Modelfile, not here; extra_args carries the
# flags `ollama run` does expose. Local CPU/iGPU inference is slow, so give it a longer timeout.
[adapters.ollama]
default_model = "qwen2.5-coder:latest"
timeout_s     = 900.0
extra_args    = ["--keepalive", "30s"]

# A local LM Studio model. Same shape: no built-in default, so set the model key (from `lms ls`).
# Sampling lives in the model's LM Studio config; extra_args carries `lms chat` flags such as --ttl.
[adapters.lmstudio]
default_model = "google/gemma-4-12b"
timeout_s     = 900.0
extra_args    = ["--ttl", "3600"]
```

---

## Saved panels (`panels.toon`)

A panel is a named, reusable set of targets -- the crew you keep reaching for. Define one in a
`panels.toon` file and reference it by name (`consensus(panel="design-roundtable")`,
`debate(panel=...)`, `review(panel=...)`) instead of listing the targets every call. Panels use
TOON, not TOML; the main `config.toml` is unchanged.

### Discovery and precedence

Three locations are searched and their panels merged by name. On a name collision the closest
scope wins, the same way a project `rutherford.toml` overrides the global `config.toml`:

```
~/.rutherford/panels.toon          (home / global; lowest priority)
        +
<cwd>/.rutherford/panels.toon      (project; overrides home for a same-named panel)
        +
$RUTHERFORD_CONFIG_DIR/panels.toon (explicit; overrides both)
```

Distinct panel names from every location are unioned, so global panels stay available even when a
project defines its own. Edits are picked up by the `reload_panels` tool without a server restart.

### File format

```toon
panels:
  design-roundtable:
    description: Lineage-diverse design review
    strategy: all-voices
    targets[3]:
      - cli: claude_code
        model: opus
        label: proposer
      - cli: codex
        label: implementer
      - cli: kiro
        model: deepseek-3.2
        label: dissenter
        stance: against
```

A panel record has `description` (optional), `strategy` (optional, default `all-voices`), and a
non-empty `targets` list. A target has `cli` (required, must be a known adapter id), `model`,
`role`, `label`, `weight`, `parity`, and `stance` (`for` | `against` | `neutral`).
`consensus`/`debate`/`review` apply a seat's `cli`, `model`, `role`, `label`, and `stance` (a
per-seat `role` overrides the call-level role, and `label` is the key the seat appears under in a
debate transcript). `weight` and `parity` feed the consensus strategies (see below), and the panel
`strategy` is adopted by a `consensus` call that uses the panel unless it passes its own.

### Consensus strategies

By default `consensus` returns every voice (`strategy: all-voices`). Any other strategy asks each
voice for a verdict and aggregates the panel into one `outcome`:

| Strategy | Aggregation | Possible outcomes |
|----------|-------------|-------------------|
| `all-voices` | Return every voice unchanged. No verdict, no aggregation. (Default.) | — |
| `unanimous` | Every eligible voice must weigh in and agree; a failed or unparseable voice vetoes. | `unanimous`, `split`, `no_quorum` |
| `majority` | A verdict must exceed 50% of **all** eligible voices (failed/unparseable count in the denominator). No verdict over the bar gives `no_majority`. | `majority`, `no_majority`, `no_quorum` |
| `plurality` | The single top-scoring verdict wins even below 50%; a tie at the top is `tied`. (This was the pre-1.1 `majority` behavior.) | `plurality`, `tied`, `no_quorum` |
| `weighted` | Like `majority` but on summed target `weight`: one verdict must exceed 50% of the total eligible weight, else `no_majority`. | `majority`, `no_majority`, `no_quorum` |
| `parity-pair` | The proposer's verdict must match every parity counterweight; a missing, failed, or disagreeing counterweight is `escalate`. | `agree`, `escalate`, `no_quorum` |

The `min_quorum` config field (default `1`) sets the minimum parseable voices an aggregating strategy
needs before it will certify an outcome. Below that floor the outcome is `no_quorum`.

A verdict is read from a final `VERDICT: <token>` line in each voice's answer. Pass a
`verdict_schema` (a JSON schema) to instead ask each voice for a JSON object containing a `verdict`
field. A voice whose answer yields no verdict is `unparseable`: still returned, but left out of the
tally. The result carries the `strategy`, the `outcome`, a `decision` (the winning verdict token, or
none), and each voice's `verdict` and full `text`. For `parity-pair`, the proposer is the seat
labeled `proposer`, or the heaviest non-parity seat.

Panel files are validated when first loaded. A bad file reports every problem at once -- malformed
TOON, an unknown `cli`, a bad `stance` -- pointing at the file and the offending target index,
rather than failing on the first error. A panel naming an unknown CLI is an error; a panel whose
CLI is installed but unauthenticated still loads, and that voice is skipped at run time with the
usual reason.

---

## Custom roles

A role is a named persona whose text becomes a system prompt (or a prompt prefix on CLIs without a
system-prompt flag). The four built-ins -- `planner`, `codereviewer`, `security`, `debugger` --
always load. Custom roles are layered on top, lowest precedence first:

1. Built-in roles.
2. Each directory in the `role_dirs` config field (`source: config`).
3. `~/.rutherford/roles/` (`source: user`).
4. `<cwd>/.rutherford/roles/` (`source: project`).
5. `$RUTHERFORD_CONFIG_DIR/roles/` (`source: env`).

A later layer overrides an earlier one by role name, so a project role beats a same-named home
role -- the same closest-scope-wins rule panels use. `list_roles` reports each role's `source`.

A role file is either markdown or TOON:

```markdown
---
name: citation-auditor
display_name: Citation Auditor
description: Third-seat auditor that checks every source.
---
You are auditing an answer for unsupported claims. For each claim, ...
```

```toon
name: citation-auditor
display_name: Citation Auditor
description: Third-seat auditor that checks every source.
system_prompt: |
  You are auditing an answer for unsupported claims. For each claim, ...
```

For markdown the body after the frontmatter is the system prompt; for TOON it is the
`system_prompt` field. The filename is the default role name when `name` is omitted. A file that
fails to parse (bad TOON, an empty body) is logged at warning level and skipped, so one malformed
role never stops the others -- or the server -- from loading.

---

## Per-CLI authentication

Rutherford never performs interactive logins. It detects auth state only. Set the relevant
environment variables before starting the server, or run each CLI's interactive login once to
establish a reusable session.

See `.env.example` in the repo root for the full list of per-CLI auth variables
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `KIRO_API_KEY`, Goose provider vars, etc.) and notes
on CLIs that use OS credential stores instead of env vars (Antigravity authenticates via
Google OAuth and has no API-key env var).
