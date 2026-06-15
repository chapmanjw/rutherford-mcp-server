# Configuration reference

Rutherford uses TOML for configuration and works with zero config. The loader discovers, merges, and
validates config at startup. A missing file is not an error; defaults apply. Invalid config (a parse
failure or a schema violation) raises `ConfigError` and the process exits non-zero before serving any
request.

The `setup` tool writes a commented starter `config.toml` at the effective defaults — the easiest way
to start.

## Discovery and precedence

When `RUTHERFORD_CONFIG` is set, that single file is used and discovery is skipped; a missing file
there fails startup. Otherwise the loader merges, from lowest to highest priority:

```
global acp.json   ->  global config.toml   ->  project acp.json   ->  project config.toml   ->  RUTHERFORD_* env
```

At a scope the native TOML wins over an imported `acp.json`; the project wins over the global; the
environment overrides specific scalar fields last. Nested dicts merge recursively; lists and scalars
replace entirely.

### Global config path

| Platform | Path |
| --- | --- |
| Windows | `%APPDATA%\rutherford\config.toml` |
| Linux / macOS | `$XDG_CONFIG_HOME/rutherford/config.toml` (fallback: `~/.config/rutherford/config.toml`) |

### Project-local config

The loader searches the working directory for these names, first found wins:

1. `rutherford.toml`
2. `.rutherford.toml`
3. `.rutherford/config.toml`

The `.rutherford/config.toml` form lives under the same project `.rutherford/` directory the `setup`
tool writes to, alongside any `acp.json` import.

> **Security.** Project-scoped config is trusted as code — it can set an agent's launch `command` and
> subprocess `env`. Discovery keys off the process working directory, so only start the server in a
> workspace you trust. The trusted-workspace gate (below) covers write/yolo *delegations*, not config
> discovery.

---

## `RutherfordConfig` fields

All fields are optional and take the listed default. This table is the complete reference, in the
order the fields appear in `config/schema.py`.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled_agents` | `list[str]` or omitted | all known + configured | Restrict the registry to these agent ids. `None` enables every built-in plus configured agent. |
| `agents` | `dict[str, AgentConfig]` | `{}` | Agent definitions and overrides keyed by id (see `AgentConfig` below). |
| `auto_detect_local_models` | `bool` | `true` | Probe a running Ollama (`:11434`) and LM Studio (`:1234`) at startup and register each tool-capable model as a `goose`-based agent. A built-in or explicit `[agents.<id>]` of the same id always wins; a down backend is skipped. |
| `default_safety_mode` | `string` | `"read_only"` | Safety posture when a call omits `safety_mode`. One of `read_only`, `propose`, `write`, `yolo`. |
| `default_timeout_s` | `float` | `300.0` | Per-run timeout in seconds (> 0). |
| `default_effort` | `string` or omitted | none | Default reasoning-effort tier when a call names none (`low` / `medium` / `high` / `xhigh`); `None` lets the agent decide. |
| `default_time_budget_s` | `float` or omitted | none | Default wall-clock budget for a panel / job; `None` means no budget (runs to completion). |
| `default_on_budget` | `string` | `"harvest"` | Disposition at a time-budget deadline when a call names none. |
| `role_dirs` | `list[str]` | `[]` | Extra directories to search for role markdown files. Built-in roles always load. Resolved to absolute paths; a missing directory warns, it does not fail. |
| `max_depth` | `int` | `3` | Maximum delegation depth (1–10) before a chain is refused. |
| `max_targets` | `int` | `8` | Maximum targets a single `consensus` / `debate` call may fan out to (1–32). |
| `max_agents_advisory` | `int` or omitted | none | Advisory aggregate-agent ceiling (≥ 2). A panel wider than this is flagged and a warning is logged, not blocked, unless `enforce_agent_cap` is set. `None` disables the check. |
| `enforce_agent_cap` | `bool` | `false` | When `true` and `max_agents_advisory` is set, refuse an over-cap panel up front with `AGENT_CAP_EXCEEDED` instead of merely warning. |
| `max_debate_rounds` | `int` | `4` | Maximum rounds a single `debate` call may run (1–10). |
| `min_quorum` | `int` | `1` | Minimum parseable voices an aggregating consensus strategy needs (≥ 1) before it returns a decision. |
| `min_distinct` | `int` | `2` | Distinct-identity floor below which a panel's answers are flagged `low_diversity` (≥ 1). |
| `max_concurrency` | `int` | `max_targets` | Ceiling on live ACP sessions run at once across a panel (≥ 1); defaults to `max_targets`. Enforced by a semaphore the delegation primitive and the panel fan-out share, so a wide panel cannot exceed it on any path. |
| `cooldown_threshold` | `int` | `3` | Unhealthy ACP failures within `cooldown_window_s` before an agent is benched (≥ 0; `0` disables cooldown). A benched agent is left out of an `expand_all` auto-panel and skipped as a fallback candidate, but an explicit `delegate` to it still runs. |
| `cooldown_window_s` | `float` | `120.0` | The sliding window over which `cooldown_threshold` failures are counted (> 0). |
| `cooldown_duration_s` | `float` | `60.0` | How long a benched agent stays benched before it is tried again (> 0). |
| `trusted_workspaces` | `list[str]` | `[]` | Absolute paths under which `write` / `yolo` delegations are permitted. Resolved to absolute; a missing directory warns. |
| `synthesize_default` | `bool` | `false` | Whether consensus synthesizes server-side by default. |
| `verify_read_only` | `bool` | `false` | Opt-in: after a successful `read_only` delegation whose `working_dir` is a git repo, fingerprint the tree under it (status + the staged and unstaged diffs, scoped to that subtree) before and after the turn and fail the result with `READONLY_VIOLATED` if it changed — catching an agent that touched the disk out of band (its own OS process, outside the ACP file callbacks). Off by default (it adds two git reads per delegation). Soundest for a single delegation; a non-git `working_dir` makes it a no-op (the fingerprint is unavailable). `write` / `yolo` / `propose` runs already execute in an isolated sandbox, so this check applies to `read_only`. |
| `probe_cache_ttl_s` | `float` | `10.0` | Seconds to cache an agent's metadata probe (≥ 0; `0` disables). |
| `probe_timeout_s` | `float` | `20.0` | Hard per-probe timeout ceiling in seconds (≥ 1), a hang guard. |
| `job_ttl_s` | `float` | `3600.0` | Seconds a finished background job is retained before eviction (≥ 1). |
| `max_jobs` | `int` | `100` | Maximum background jobs retained at once (≥ 1). Past the cap, creating one fails with `TOO_MANY_JOBS`. |
| `default_persistence` | `string` | `"ephemeral"` | Whether a run is persisted to disk by default (`ephemeral` / `job`). |
| `jobs_dir` | `string` or omitted | `<cwd>/.rutherford/jobs` | Where durable jobs are written. |
| `log_level` | `string` | `"info"` | Structured-log verbosity (`debug` / `info` / `warning` / `error`). Logs go to stderr as JSON. |
| `log_format` | `string` | `"json"` | Structured-log format (`json` / `off`). stdout is the MCP channel and is never written to. |

> Two fields are part of the config contract but are **not yet wired** into the leaner v3 path — they
> validate and load, but have no effect today, and land as those features are re-added over the ACP core.
> The not-yet-active set: `probe_cache_ttl_s`, `probe_timeout_s` (metadata-probe caching).
>
> What **is** active today: the roster fields (`agents`, `enabled_agents`, `auto_detect_local_models`),
> `default_safety_mode` and `trusted_workspaces` (read_only is the default and the write/yolo trust gate
> is enforced; `write` / `propose` / `yolo` run in an isolated git-worktree sandbox and `verify_read_only`
> checks a `read_only` run did not mutate its git tree), `default_timeout_s`, `default_effort`,
> `default_time_budget_s`, `default_on_budget` (time budget / effort), `default_persistence` / `jobs_dir`
> (F2 durable on-disk jobs — a `persist=true` `delegate` / `consensus` / `debate` writes a `state.json`
> record plus Markdown artifacts under `jobs_dir`; the in-memory `JobStore` is the separate async-job
> runtime), `max_targets`, `max_depth` (the recursion guard, `MAX_DEPTH_EXCEEDED`), `max_concurrency` (the
> fan-out semaphore), `max_agents_advisory` / `enforce_agent_cap` (the aggregate-agent cap — flags
> `Topology.over_cap`, or refuses with `AGENT_CAP_EXCEEDED` when enforced), `min_quorum`, `min_distinct`,
> `synthesize_default` (consensus aggregation / synthesis / diversity), `cooldown_threshold` /
> `cooldown_window_s` / `cooldown_duration_s` (the F7 cooldown / quarantine — bench a flapping agent out of
> auto-selection and fallback), `max_debate_rounds`, `role_dirs`, the in-memory job knobs (`job_ttl_s`,
> `max_jobs`), and the logging fields.

### `AgentConfig` fields (under `[agents.<id>]`)

An `[agents.<id>]` entry overrides a built-in agent, defines a brand-new agent, or clones a built-in
and points it at a local runtime. An id matching a built-in overrides its fields; an id that does not
match a built-in defines a new agent and must supply `command` (or a `base`). The launch fields mirror
the Zed/Cline `acp.json` shape.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | `bool` | `true` | Set to `false` to remove this agent from the registry. |
| `command` | `list[str]` or omitted | none | The launch argv for this agent's ACP server. Required to define a new agent; for a built-in it replaces the default launch command. Mutually exclusive with `base` / `backend`. |
| `env` | `dict[str, str]` | `{}` | Environment variables set for the agent subprocess (layered on the inherited environment). Populated from an `acp.json` `env` block. |
| `provider` | `string` or omitted | the built-in's | The fixed model vendor, recorded as provenance. |
| `default_model` | `string` or omitted | the built-in's | The model used when a call names none. |
| `handshake_timeout_s` | `float` or omitted | the built-in's (30s for a new agent) | Seconds for the `initialize` + `new_session` handshake (> 0). Raise it for a heavyweight agent. |
| `timeout_s` | `float` or omitted | the global `default_timeout_s` | Per-agent run timeout (> 0) when a call names no `timeout_s`. |
| `extra_args` | `list[str]` | `[]` | Extra arguments appended to the launch argv. |
| `effort` | `string` or omitted | the global `default_effort` | Per-agent default reasoning-effort tier; a no-op for an agent with no effort knob. |
| `fallback_model` | `string` or omitted | none | The model to retry with when the requested model is unavailable (F7 model fallback). `None` means this agent exposes no fallback model, so a model-unavailable failure does not retry it on another model. Most ACP agents cannot decline a named model, so this stays unset for them. |
| `base` | `string` or omitted | none | Clone a *built-in* agent's launch command under this new id (e.g. `base = "goose"`). Mutually exclusive with `command`. |
| `backend` | `"ollama"` / `"lmstudio"` or omitted | none | Point this agent at a local model runtime. Requires `model`. See [local-models.md](local-models.md). |
| `model` | `string` or omitted | none | The model id served by `backend` (required when `backend` is set); becomes the agent's default model. |
| `host` | `string` or omitted | `localhost:11434` (Ollama) / `localhost:1234` (LM Studio) | The `backend` endpoint as `host:port`. |

---

## Importing a Zed/Cline `acp.json`

The loader auto-discovers an `acp.json` beside the global config and in the project's `.rutherford/`,
and folds its `agent_servers` block into the agents config the way Zed and Cline read it. Only the
launch `command` and `env` are imported, so the import stays minimal.

- The native TOML wins over an imported `acp.json` at the same scope.
- An imported agent whose id collides with a built-in is skipped — override a built-in explicitly in
  `[agents.<id>]` instead, never by silent import.
- A malformed `acp.json` is logged and skipped, never a startup crash (unlike a malformed TOML config,
  which is a hard error).

This lets a workspace that already configures ACP agents for Zed or Cline reuse that configuration
with no extra work.

---

## Environment overrides

These override specific fields after the config files are merged. They do not replace the whole config.

| Variable | Type | Overrides |
| --- | --- | --- |
| `RUTHERFORD_CONFIG` | path | Replaces file discovery; must point to an existing file. |
| `RUTHERFORD_MAX_DEPTH` | integer | `max_depth` |
| `RUTHERFORD_MAX_TARGETS` | integer | `max_targets` |
| `RUTHERFORD_MAX_CONCURRENCY` | integer | `max_concurrency` |
| `RUTHERFORD_DEFAULT_TIMEOUT_S` | float | `default_timeout_s` |
| `RUTHERFORD_DEFAULT_SAFETY` | string | `default_safety_mode` |
| `RUTHERFORD_TRUSTED_WORKSPACES` | `os.pathsep`-delimited paths | `trusted_workspaces` |
| `RUTHERFORD_ROLE_DIRS` | `os.pathsep`-delimited paths | `role_dirs` |

---

## Example

```toml
# config.toml

# Restrict the registry to three agents.
enabled_agents = ["claude_code", "codex", "goose"]

default_safety_mode = "read_only"
default_timeout_s   = 120.0
max_targets         = 4

# Absolute paths under which write/yolo delegations are permitted.
trusted_workspaces = [
    "/home/user/projects/myapp",
]

# Extra directories to search for role markdown files.
role_dirs = ["/home/user/.config/rutherford/roles"]

# Override a built-in agent's default model.
[agents.claude_code]
default_model = "claude-sonnet-4-6"

# Disable a built-in without dropping it from enabled_agents.
[agents.codex]
enabled = false

# Define a brand-new agent from a launch command.
[agents.my-agent]
command = ["node", "/abs/path/to/agent.js"]
provider = "openai"

# A local Ollama model as a first-class voice (see docs/local-models.md).
[agents.local-goose]
base    = "goose"
backend = "ollama"
model   = "qwen3:8b"
```

---

## Roles

A role is a named persona whose text is prepended to the caller's prompt with a `---` delimiter. Pass
`role="<id>"` to `delegate` / `consensus` / `debate`. Five built-ins ship as package data:

| id | persona |
| --- | --- |
| `principal-reviewer` | a rigorous senior code reviewer who separates must-fix from nits |
| `architect` | a system designer who weighs tradeoffs and names the failure modes |
| `debugger` | a root-cause debugger who proposes the smallest correct fix |
| `security-reviewer` | a threat-modeling reviewer who rates findings by severity |
| `explainer` | a clear teacher who explains code from the reader's understanding |

A `role_dirs` directory adds new roles or overrides a built-in of the same id. Each role file is
markdown with a small `name` / `description` frontmatter block; the body is the system prompt. Loading
is tolerant — a missing directory or a malformed file is logged and skipped, never a startup crash.
`list_roles` enumerates the catalog; a bad `role` id fails on the request path with `UNKNOWN_ROLE`.

---

## Per-agent authentication

Rutherford never performs interactive logins; it reuses each agent's own login. Sign in with each
agent's own flow (or set its API key) before starting the server, so the headless ACP session can
reuse the session. `codex` (`codex-acp`) and `claude_code` (`claude-agent-acp`) reuse the existing
Codex and Claude Code CLI logins over ACP and need no API key. Confirm what actually drives on this
machine with `doctor`.
