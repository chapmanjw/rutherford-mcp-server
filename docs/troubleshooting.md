# Troubleshooting

## First step for any failure

Run `doctor` before investigating further. It drives each agent with a real read-only ACP round trip
— the only trustworthy health signal — and reports `ok`, `no_answer`, `model_unavailable`,
`handshake_failed`, `not_installed`, or `error` per agent. `capabilities` is the cheaper read (the
static registry: launch command, `default_model`, `model_selection`, `effort_capable`; no spawn).
For live advertised model ids use `doctor(agent=<id>, connect_only=true)`.

---

## Symptoms

### `not_installed` in doctor / `ACP_SPAWN_FAILED`

The agent's launch command was not found on PATH, or the executable failed to start. `ACPSession.open`
spawns the agent as an ACP server; a missing binary, a `working_dir` that resolves to a file, or an
unexecutable command all surface as `ACP_SPAWN_FAILED`, which `doctor` classifies as `not_installed`.

- Install the agent (or its ACP shim — `codex` needs `codex-acp`, `claude_code` needs
  `claude-agent-acp`, `pi` needs `pi-acp`, all `npm i -g`).
- **The CLI is installed but the adapter shim is not.** `codex` / `claude_code` / `pi` launch a *separate*
  npm adapter that fronts the underlying CLI (`codex` / `claude` / `pi`). If you have the CLI but the shim
  is missing, `doctor` does not just say `not_installed` — it adds an `install_hint` with the exact
  `npm i -g <package>`. Run that, or let Rutherford do it: **`setup install_adapters=true`** detects every
  such gap (CLI present, shim absent) and runs the install for you. `setup` (no flag) lists them under
  `adapters.installable` without installing.
- If it is installed but not on the server's PATH, add its directory to PATH before starting the
  Rutherford process.
- On Windows, confirm the resolved command is a real `.exe` or a recognized npm shim. `prepare_argv`
  resolves npm shims to their real target; a non-npm `.bat` falls back to PowerShell, then `cmd /c`.

### `handshake_failed` in doctor / `ACP_HANDSHAKE_FAILED`

The agent spawned but the `initialize` / `new_session` handshake failed — a protocol mismatch, an auth
failure surfacing at session creation, or a slow setup that blew the handshake budget.

- Confirm the agent actually supports ACP and the launch command is the ACP entry point (for example,
  `cursor-agent acp`, not `cursor-agent`; `kiro-cli acp`, not the `kiro` IDE launcher).
- A heavyweight agent that sets up a workspace on `new_session` (OpenHands) may need a larger
  `handshake_timeout_s`. Set `[agents.<id>] handshake_timeout_s = 90` in config.
- Some agents drive over ACP only with their own service auth. `cline`, for instance, returns an empty
  handshake/turn when configured for a ChatGPT subscription or OpenRouter in the desktop app — its
  headless `--acp` path needs Cline's own service auth.

### `no_answer` in doctor / `ACP_REFUSED` / `ACP_EMPTY_ANSWER`

The agent spawned and handshook but ended the turn without an answer — it refused, or produced no
text. Often an auth or model-availability problem that only shows once the agent tries to call its
model.

- Sign in to the agent with its own login, or set its API key, then re-run `doctor`.
- For a local-model agent, confirm the model supports tool-calling — a model without it handshakes but
  fails the agentic turn. See [local-models.md](local-models.md).

### `model_unavailable` in doctor — `claude_code` 400 invalid model on Bedrock / Amazon Toolbox

The seat spawned and handshook (it shows `reachable` under `doctor connect_only`), but the turn failed
because the provider rejected the model id:

```
API Error (claude-opus-4-8): 400 The provided model identifier is invalid.
```

This is a Claude Code configured for **AWS Bedrock** / **Google Vertex**, or an enterprise wrapper such
as **Amazon's Toolbox** build, where the third-party `claude-agent-acp` adapter resolves the model down
to a bare cloud alias (`claude-opus-4-8`) that the provider rejects — it needs an inference-profile id
like `us.anthropic.claude-opus-4-1-20250805-v1:0`. The standalone `claude` CLI works because it resolves
the Bedrock model itself; the SDK/adapter path that Rutherford drives does not. `doctor` attaches a
`remediation_hint` for this case.

The fix is a per-agent `[agents.claude_code.env]` block in Rutherford's own config — it lives outside the
`.claude` tree, so an enterprise wrapper that rewrites `settings.json` cannot revert it, and
`ANTHROPIC_CUSTOM_MODEL_OPTION` survives an enforced model allowlist:

```toml
[agents.claude_code]
default_model = "global.anthropic.claude-opus-4-8[1m]"

[agents.claude_code.env]
ANTHROPIC_MODEL = "global.anthropic.claude-opus-4-8[1m]"
ANTHROPIC_CUSTOM_MODEL_OPTION = "global.anthropic.claude-opus-4-8[1m]"
```

Reconnect the MCP server (config is read once at start) and re-run `doctor agent=claude_code`. See
**[Claude Code on Bedrock / enterprise wrappers](bedrock.md)** for the full mechanism and the approaches
that do *not* work.

### `ACP_TURN_TIMEOUT` — the turn exceeded its limit

`ACPSession.prompt` wraps the turn in a timeout; on expiry it issues `session/cancel`, preserves any
streamed partial answer on `result.partial`, and fails as `ACP_TURN_TIMEOUT`. The session's descendant
process tree is reaped on close.

- Raise the per-call `timeout_s` on the `delegate` / `consensus` / `debate` call.
- Raise `default_timeout_s` in config (default 300s), or set a per-agent `[agents.<id>] timeout_s` for
  one slow agent.
- For long tasks, use `mode="async"`: the call returns a job id immediately and you poll with
  `job_status` / `job_result`. The timeout still applies to the underlying turn.
- A local model on CPU/iGPU is slow, and the first call is slowest (cold weight-load). Pre-load the
  model and give the agent a generous timeout. See [local-models.md](local-models.md).

### `hermes` is slow or times out intermittently

`hermes` is registered and drives over ACP, but the Nous endpoint's latency swings widely. It is
deliberately kept out of the bounded integration test. Check it live with `doctor`, and give it a
longer `timeout_s` if you use it in a panel.

### `WORKSPACE_NOT_TRUSTED` — write or yolo refused

A `write` or `yolo` delegation is mutating. Before spawning, `DelegationService._workspace_trusted`
checks whether `working_dir` is under a configured `trusted_workspaces` path or whether the call passed
`trust_workspace=true`. If neither holds, the delegation fails with `WORKSPACE_NOT_TRUSTED` and no
agent is spawned. A delegation that omits `working_dir` also fails.

- Pass `trust_workspace=true` on the call (per-call opt-in), or
- Add the directory to `trusted_workspaces` in config:

```toml
trusted_workspaces = ["/home/user/projects/myapp", "C:\\Users\\user\\projects\\myapp"]
```

From the repo root, the one-shot CLI registers cwd in the **global** allowlist:

```sh
rutherford trust                 # or: python -m rutherford trust [/path]
rutherford untrust               # remove cwd (or a path) from the global allowlist
rutherford trust --list
```

Or set `RUTHERFORD_TRUSTED_WORKSPACES` (paths separated by `;` on Windows, `:` on POSIX).

### `UNKNOWN_TARGET` — agent id not recognized

The `cli` (or a `targets` entry's `cli`) does not match any registered agent id. The registry is a
closed mapping; an unknown id fails with `UNKNOWN_TARGET` and lists the known ids.

- Run `capabilities` to see every registered agent id. The built-in ids are `goose`, `opencode`,
  `vibe`, `cline`, `junie`, `kimi`, `openhands`, `codex`, `claude_code`, `copilot`, `qwen`, `droid`,
  `cursor`, `kiro`, `pi`, `hermes`, `gemini`, `qoder`, `grok`, plus any you added in config and any
  auto-detected local model (`ollama-<model>` / `lmstudio-<model>`). The id is case-sensitive.

### `TOO_MANY_TARGETS` — panel fan-out exceeds the cap

A `consensus` / `debate` call lists more targets than `max_targets` (default 8). The call is refused
before any agent is spawned.

- Reduce the targets, or raise `max_targets` in config (or `RUTHERFORD_MAX_TARGETS`).

### `UNKNOWN_ROLE` — named role does not exist

The `role` argument named a persona the `RoleStore` does not know. Five built-ins always load
(`principal-reviewer`, `architect`, `debugger`, `security-reviewer`, `explainer`); a custom role needs
a `role_dirs` entry pointing at the directory with the `.md` file.

- Run `list_roles` to see what is loaded. Add your directory to `role_dirs`:

```toml
role_dirs = ["/home/user/.rutherford/roles"]
```

### `JOB_NOT_FOUND` — polling a job that no longer exists

Background jobs live in memory with a TTL set by `job_ttl_s` (default 3600s). A finished job is evicted
after the TTL on the next access, and a server restart clears all jobs.

- Collect the result promptly once `job_status` reports `succeeded` or `failed`.
- Confirm the job id is passed back correctly — it is a 12-char hex string from the submit envelope.

### `TOO_MANY_JOBS` — background-job cap reached

The store is full and every retained job is still running, so there is nothing safe to evict. The new
submission is refused with `TOO_MANY_JOBS`.

- Let some jobs finish, or `cancel_job` ones you no longer need, or raise `max_jobs` in config.

### Server exits immediately / `ConfigError` on startup

`ConfigError` is fatal and surfaces before the server serves. It is raised when a TOML config cannot be
parsed or when the merged config fails validation. The message lists the failures:

```
invalid configuration:
  - max_depth: Input should be a valid integer
  - agents.my-agent: agent 'my-agent' is not a built-in agent and has no 'command' or 'base' ...
```

Read the field path and fix the corresponding key. A malformed `acp.json` is *not* fatal — it is
logged and skipped. A non-UTF-8 config file (the UTF-16 that some Windows redirection writes) is
reported as a `ConfigError`, not a raw decode error.

---

## Quick reference

| Code | Immediate diagnostic |
| --- | --- |
| `ACP_SPAWN_FAILED` | Run `doctor`; install the agent (and its ACP shim); confirm it is on PATH. |
| `ACP_HANDSHAKE_FAILED` | Confirm the ACP launch command; raise `handshake_timeout_s`; check the agent's auth. |
| `ACP_REFUSED` / `ACP_EMPTY_ANSWER` | The agent answered nothing; check auth, or a local model's tool-calling support. |
| `model_unavailable` (doctor) | The provider rejected the model id; on Bedrock/Vertex/Toolbox pin one via `[agents.claude_code.env]` — see [bedrock.md](bedrock.md). |
| `ACP_TURN_TIMEOUT` | Raise `timeout_s` or `default_timeout_s`; use `mode="async"` for long tasks. |
| `ACP_TURN_ERROR` | A transport/protocol error mid-turn; re-run, and check `doctor`. |
| `WORKSPACE_NOT_TRUSTED` | Pass `trust_workspace=true` or add the path to `trusted_workspaces`. |
| `UNKNOWN_TARGET` | Run `capabilities` to list registered agent ids; the id is case-sensitive. |
| `TOO_MANY_TARGETS` | Reduce targets or raise `max_targets` (default 8). |
| `UNKNOWN_ROLE` | Run `list_roles`; add your directory to `role_dirs`. |
| `JOB_NOT_FOUND` | Collect results before TTL (`job_ttl_s`, default 1h); jobs clear on restart. |
| `TOO_MANY_JOBS` | Let jobs finish, `cancel_job`, or raise `max_jobs`. |
| `READONLY_VIOLATED` | A `read_only` / `propose` run changed the git tree; check the run, or disable `verify_read_only`. |
