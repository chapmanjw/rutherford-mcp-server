# Troubleshooting

## First step for any delegation failure

Run the `doctor` tool (or `capabilities` for a lighter read) before investigating further. `doctor` calls every registered adapter's `detect`, `check_auth`, and `available_models` non-destructively and emits actionable notes. The output tells you which CLIs are missing, which need a credential, and which are ready. Passing `include_raw=true` to any `delegate` or `consensus` call adds the raw stdout/stderr to the result envelope under the `raw` field -- useful when the error code alone is not enough to diagnose a parse failure.

---

## Symptoms

### `BINARY_NOT_FOUND` -- target CLI not installed or not on PATH

**Cause.** `DelegationService.delegate` calls `adapter.detect()` before spawning anything. If `shutil.which` cannot find the binary, the delegation returns `ok=false` with `error.code=BINARY_NOT_FOUND` immediately; no subprocess is started.

**Fix.**
1. Run the `doctor` tool. The note for any uninstalled adapter reads: `<binary> was not found on PATH; install it (see docs/integration-testing.md)`.
2. Install the CLI following `docs/integration-testing.md`.
3. If the CLI is installed but not on `PATH`, either add its directory to `PATH` before starting the Rutherford server process, or specify its full path via a `generic_adapters` entry in your config.

---

### `AUTH_REQUIRED` / auth note in doctor -- target needs a credential

**Cause.** Rutherford never performs an interactive login on your behalf. `check_auth` for each adapter is a read-only probe: it looks for an API key env var or a persisted session file. When neither is present the probe returns `AuthState.NEEDS_LOGIN` (or `API_KEY_MISSING`), and `doctor` adds a note.

The auth mechanism differs per CLI:

| CLI | What to do |
|-----|-----------|
| `claude_code` | Set `ANTHROPIC_API_KEY`, or run `claude auth login` once |
| `codex` | Set `OPENAI_API_KEY` or `CODEX_API_KEY`, or run `codex login` (persists to `~/.codex/auth.json`) |
| `opencode` | Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, or run `opencode auth login` |
| `goose` | Set `GOOSE_PROVIDER` plus a provider key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.), or run `goose configure` |
| `kiro` | Set `KIRO_API_KEY`, or run `kiro-cli login` |
| `antigravity` | Run `agy` once interactively to complete the Google OAuth flow |

`agy` exposes no `whoami`, and where it stores its token varies by platform and install, so a cheap
probe cannot determine its auth state -- `capabilities` reports it as `unknown`. `doctor` resolves
that by default (`live=true`): for any installed adapter still `unknown`, it runs a minimal
read-only round trip and reports `authenticated` or `needs_login` from the outcome. That spends a
small model call; pass `live=false` for a metadata-only `doctor` with no model calls.

#### Third-party model backends (AWS Bedrock, Google Vertex)

`claude_code` and `codex` can be pointed at a cloud backend instead of their native API: Claude
Code via `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` / `CLAUDE_CODE_USE_MANTLE`
(authenticated by the AWS/GCP credential chain), and Codex via a `model_provider = "amazon-bedrock"`
config or any custom provider. In that mode there is **no** `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
and **no** native login session -- so the old "is the API key or login present" probe wrongly
reported `needs_login` even though the CLI works.

The adapters now read the CLI's own effective-auth signal instead:

- `claude_code` parses the JSON body of `claude auth status`. When it names a third-party backend
  (`apiProvider` like `bedrock`, `authMethod: "third_party"`, or a `CLAUDE_CODE_USE_*` switch),
  `loggedIn: true` only proves the backend is *configured*, not that the AWS/GCP credentials can
  reach a model -- so the cheap probe returns `unknown` and `doctor`'s live round trip confirms it.
- `codex` reads `codex doctor --json` and trusts its `checks["auth.credentials"].status`, which
  already validates the effective credential (including a Bedrock provider). No live call is needed.

So a Bedrock/Vertex CLI shows as `unknown` under `capabilities` (and `doctor live=false`) and
resolves to `authenticated` / `needs_login` under the default `doctor`. If it resolves to
`needs_login`, the live call actually failed: check `AWS_REGION` is set, the AWS profile/SSO session
is current (`aws sts get-caller-identity`), and the account has Bedrock model access.

Put API keys in a `.env` file (confirmed gitignored -- see `SECURITY.md`) and load them into the server process environment. Rutherford inherits the parent process environment unchanged.

---

### Windows: `.cmd` / npm-shim launch issues

**Cause.** On Windows, several CLIs (`codex`, `opencode`, and others installed via npm) resolve to `.cmd` shims. `CreateProcess` cannot launch a `.cmd` file directly -- it would raise `WinError 193`. Rutherford handles this via `runtime/launch.py`: `prepare_argv` calls `shutil.which` (respecting `PATHEXT`) to resolve the binary, then wraps any non-`.exe` result as `[cmd.exe, /c, <resolved>, *rest]`. Arguments remain separate list elements; no shell string is assembled.

For `codex` specifically, the prompt is passed on **stdin** (not as an argv element) precisely to avoid quoting issues through the `cmd.exe` layer.

**What this means in practice.** The wrapping is automatic and transparent. If you see a `NONZERO_EXIT` that looks like a shell-invocation error (e.g. "not recognized as an internal or external command"), confirm the CLI is actually on `PATH` via `where codex` (or `Get-Command codex` in PowerShell) and that the resolved path ends in `.cmd` or `.exe`. Run the `doctor` tool to confirm Rutherford finds it.

---

### Windows: `codex` answers from the prompt only / `windows sandbox: spawn setup refresh` / `blocked by policy`

**Cause.** On Windows, Codex sandboxes every shell command it runs, and `codex exec` runs under an approval policy. Two defaults break a headless, nested `codex exec` (the kind Rutherford spawns):

- Codex's default `[windows] sandbox = "elevated"` (in `~/.codex/config.toml`) needs a UAC/administrator setup step that a non-interactive child process cannot complete, so every tool call fails with `windows sandbox: spawn setup refresh`.
- The default approval policy blocks any command Codex deems "untrusted" with `rejected: blocked by policy`, because a headless run has no one to approve.

With either in play Codex cannot read files or run commands and silently degrades to answering from the prompt alone -- which can look like a normal (but uninformed) response in a consensus or debate.

**Fix (built in).** The `codex` adapter passes `-c approval_policy=never` for the read_only/propose/write modes and, on native Windows, `-c windows.sandbox=unelevated` (Codex's documented fallback sandbox -- a restricted token, no admin setup). The read-only sandbox still prevents writes; `approval_policy=never` only removes a prompt nothing could answer. No configuration is required.

**If you still see it,** confirm your Codex build supports the `unelevated` Windows sandbox and `-c` config overrides, and run `doctor`. Codex's trusted-command classifier may still decline a *complex* command under `approval_policy=never` (it runs simple reads and retries simpler variants), which is expected. You can also set `[windows] sandbox = "unelevated"` and `approval_policy = "never"` in your own `~/.codex/config.toml` to make every Codex invocation (interactive included) behave the same way.

---

### WSL path issues -- wrong working directory passed to a Linux CLI

**Cause.** When a generic adapter has `runtime = "wsl_interop"`, or when a native Windows host invokes a Linux CLI via WSL interop, `runtime/paths.py` translates paths between Windows and WSL forms before building the `InvocationSpec`. `translate_path` converts `C:\Users\x` to `/mnt/c/Users/x` when running on Windows targeting a WSL runtime, and the reverse when running on WSL targeting a native Windows runtime.

If a working directory arrives in the wrong form (e.g. a Windows path reaching a Linux binary), the agent either silently operates from its home directory or exits non-zero.

**Fix.**
- For built-in adapters the translation is automatic.
- For a `generic_adapters` entry, set `runtime = "wsl_interop"` when the binary runs under WSL. The path translation then applies automatically. Without it, Windows paths are passed as-is to the Linux process.
- If a CLI you are wrapping is purely native, leave `runtime = "native"` (the default). Explicitly setting `wsl_interop` on a native binary will mistranslate paths.
- WSL detection uses `WSL_DISTRO_NAME` (env var) and `/proc/version` (Microsoft string). If neither is present, the host is treated as non-WSL Linux. Both detection paths are visible in `runtime/platform.py`.

---

### `TIMEOUT` -- run exceeded its limit and was killed

**Cause.** `AsyncProcessRunner.run` wraps the subprocess in `asyncio.wait_for` with the configured timeout. On expiry, `_kill_process_tree` terminates the process and all descendants via `psutil` (terminate then kill, with a 3-second grace period). The returned `ProcessResult` has `timed_out=True`, and the adapter maps this to `ErrorCode.TIMEOUT`.

**Fix.**
- Raise the per-call `timeout_s` parameter on the `delegate` or `consensus` call.
- Raise `default_timeout_s` in config (default is `300.0` seconds). Set it in your `rutherford.toml` / `%APPDATA%\rutherford\config.toml`, or via the `RUTHERFORD_DEFAULT_TIMEOUT_S` env var. For a single slow adapter, set a per-adapter `[adapters.<id>] timeout_s` instead of raising the global default.
- For long-running tasks, use `mode="async"` on the `delegate` call: it returns a job id immediately and you poll via `job_status` / `job_result`. The timeout still applies to the underlying run, so raise it accordingly.

**Local models (Ollama, LM Studio) are a special case.** `ollama run` is a thin client for the separate `ollama serve` daemon; the generation runs inside the daemon, which is *not* a child of the process Rutherford spawns. A `TIMEOUT` kills the `ollama run` client and Rutherford stops waiting, but the daemon keeps generating to completion -- on a CPU/iGPU-only machine the fans keep spinning after the timeout is reported. To free the machine immediately, run `ollama stop <model>`; to bound how long a model stays resident, set a short `OLLAMA_KEEP_ALIVE` (e.g. `30s`) for the daemon. The first call to a model is also the slowest (cold weight-load, plus a pull if it is not present), so prefer a generous `[adapters.ollama] timeout_s`.

LM Studio shares the cold-load cost but not the detach problem: `lms chat <model> -p` JIT-loads the model in the process Rutherford spawns, so a `TIMEOUT` stops generation cleanly (no `ollama stop` analogue is needed). Its first call is still the slowest, so pre-load with `lms load <model>` and raise `[adapters.lmstudio] timeout_s` (per-adapter) or the per-call `timeout_s`.

---

### `WORKSPACE_NOT_TRUSTED` -- write or yolo refused

**Cause.** Any delegation with `safety_mode="write"` or `safety_mode="yolo"` is a mutating operation (`is_mutating` in `domain/enums.py`). Before spawning, `DelegationService._workspace_trusted` checks whether the working directory is under a configured `trusted_workspaces` path or whether the caller passed `trust_workspace=true`. If neither condition holds, the delegation returns `ok=false` with `WORKSPACE_NOT_TRUSTED` and no subprocess is started.

This is intentional: write-mode agents can modify files, and Rutherford requires an explicit acknowledgment that the workspace is safe to mutate.

**Fix.** Two options:

1. Pass `trust_workspace=true` in the individual `delegate` call (per-call opt-in).
2. Add the directory (or its parent) to `trusted_workspaces` in config so every write-mode call to that tree is permitted without a per-call flag:

```toml
# rutherford.toml
trusted_workspaces = ["/home/user/projects/myapp", "C:\\Users\\user\\projects\\myapp"]
```

Or set the env var:

```
RUTHERFORD_TRUSTED_WORKSPACES=/home/user/projects/myapp
```

On Windows, multiple paths are separated by `;`; on POSIX, by `:`.

---

### `MAX_DEPTH_EXCEEDED` -- delegation chain too deep

**Cause.** When a CLI delegates to Rutherford, which delegates to another CLI, and so on, the chain could recurse indefinitely. Rutherford propagates the current depth through the `RUTHERFORD_DEPTH` environment variable (defined in `runtime/depth.py`). Before spawning, `ensure_within_depth` checks whether `depth >= max_depth` and raises `DepthLimitError` if so. The default `max_depth` is `3`.

**Fix.**

Raise `max_depth` in config if your legitimate use requires deeper chains:

```toml
# rutherford.toml
max_depth = 5
```

Or via env: `RUTHERFORD_MAX_DEPTH=5`.

Raise this value carefully. A self-referential prompt can still recurse to the new limit. The depth limit exists precisely because these agents can and do call back into MCP tools.

---

### `TOO_MANY_TARGETS` -- consensus fan-out exceeds the cap

**Cause.** A `consensus` call lists more targets than `max_targets` allows. `ensure_within_target_cap` in `runtime/depth.py` raises before any subprocess starts. The default cap is `8`.

**Fix.**

Reduce the number of targets, or raise `max_targets` in config:

```toml
# rutherford.toml
max_targets = 12
```

Or via env: `RUTHERFORD_MAX_TARGETS=12`.

---

### `TRANSCRIPT_NOT_FOUND` -- Antigravity returned no result

**Cause.** The `antigravity` adapter (`agy`) does not use stdout for its answer; `agy -p` stdout is unreliable. Instead, `AntigravityAdapter.parse_output` reads the transcript file at `~/.gemini/antigravity-cli/brain/<conv-id>/.system_generated/logs/transcript.jsonl`. The conversation id is resolved by looking up the working directory in `cache/last_conversations.json`, falling back to the most recently modified `brain/` subdirectory. If neither the index nor a brain entry exists, or if the transcript has no line matching `source=MODEL, status=DONE, type=PLANNER_RESPONSE` with non-empty content, the result is `TRANSCRIPT_NOT_FOUND`.

The transcript schema is community-reverse-engineered (not formally documented by Google). It was verified against `agy 1.0.2` on 2026-05-30.

**Fixes.**

- **Pin the `agy` version.** A schema change in a newer release will silently stop producing parseable results. If you upgrade `agy` and start seeing this error, open an issue.
- **Serialize concurrent `agy` calls.** The `last_conversations.json` index is written by `agy` per workspace and is not locked. Running two `agy` processes against the same workspace concurrently can corrupt the index. Use a single `delegate` call (not parallel `consensus`) when targeting `antigravity`, or ensure each call uses a distinct working directory.
- **Inspect the raw output.** Pass `include_raw=true` on the delegation call. The `raw` field will contain whatever `agy` wrote to stdout and stderr, which may include an error message.
- **Check the brain directory.** Verify `~/.gemini/antigravity-cli/brain/` is populated after a run. On Windows this is `%USERPROFILE%\.gemini\antigravity-cli\` (not `%APPDATA%`).

---

### `PARSE_ERROR` -- opencode or goose output changed shape

**Cause.** The `opencode` and `goose` JSON schemas are not formally versioned. Both adapters parse defensively -- unknown fields are skipped and the parsers never raise -- but a structural change in event types or field names can produce an empty `text` that falls through to `PARSE_ERROR`.

From `.research/build-decisions.md` (2026-05-30): "opencode/goose JSON schemas are not formally versioned -> parse defensively, keep `raw`."

**Fix.**

1. Pass `include_raw=true` and inspect the `raw` field to see what the CLI actually emitted.
2. Check whether the CLI was updated since the adapter was written (`opencode` flags verified 2026-05-30; `goose` flags verified 2026-05-30).
3. If the schema changed, open an issue or update `parse_output` in `src/rutherford/adapters/opencode.py` or `src/rutherford/adapters/goose.py`.

For `goose` specifically: `GOOSE_MODE=auto` is reportedly ignored when the `claude-code` provider is configured (upstream Goose issue as of 2026-05-30). If `goose` prompts for approval instead of running headlessly, that is an upstream provider interaction, not a Rutherford bug.

---

### `JOB_NOT_FOUND` -- polling a job that no longer exists

**Cause.** Background jobs live in memory with a TTL set by `job_ttl_s` (default `3600` seconds / one hour). After a job reaches a terminal status (`succeeded`, `failed`, or `cancelled`) and the TTL elapses, it is evicted on the next `get` call. Calling `job_status` or `job_result` after eviction returns `JOB_NOT_FOUND`.

Jobs are also lost if the Rutherford server process restarts, since nothing is persisted to disk.

**Fix.**

- Collect the result promptly after polling indicates `succeeded` or `failed`.
- If you need results to survive a process restart, collect and persist them yourself before the TTL window closes.
- If you are seeing spurious `JOB_NOT_FOUND` on a job you just submitted, verify the job id is being passed back correctly -- the id is a hex `uuid4` string returned in the `Job` envelope.

---

### `ROLE_NOT_FOUND` -- named role does not exist

**Cause.** The `role` parameter on a `delegate` or `consensus` call names a role that `RoleStore` cannot find in any of the configured role directories. Built-in roles always load; custom roles require a `role_dirs` entry pointing at the directory containing the `.md` file.

**Fix.** Add the directory containing your role files to `role_dirs` in config:

```toml
# rutherford.toml
role_dirs = ["/home/user/.rutherford/roles"]
```

Or via env: `RUTHERFORD_ROLE_DIRS=/home/user/.rutherford/roles`.

Run the `roles` tool to list all currently visible roles and confirm yours appears.

---

### Server exits immediately / `ConfigError` on startup

**Cause.** `ConfigError` is fatal and surfaces before the server starts serving. It is raised by `load_config` when a config file cannot be parsed (bad TOML) or when the merged config fails Pydantic validation.

**Fix.**

The error message includes a list of validation failures in the form:

```
invalid configuration:
  - max_depth: Input should be a valid integer
  - trusted_workspaces.0: ...
```

Read the field path and fix the corresponding key in your `rutherford.toml` or `%APPDATA%\rutherford\config.toml`. Config precedence (lowest to highest): global file, project-local `rutherford.toml` / `.rutherford.toml` in the working directory, then `RUTHERFORD_*` env vars.

---

### `UNKNOWN_TARGET` -- adapter id not recognized

**Cause.** The `cli` field in a `Target` does not match any registered adapter id. The registry is a closed mapping; an unknown id raises `RegistryError` with `ErrorCode.UNKNOWN_TARGET` and lists the known ids.

**Fix.** Use the `capabilities` tool to see every registered adapter id. The built-in ids are: `claude_code`, `codex`, `cursor`, `qwen`, `kiro`, `opencode`, `goose`, `ollama`, `lmstudio`, `antigravity`. Config-defined generic adapters are registered under their configured `id` field. Check spelling -- the id is case-sensitive.

---

## Quick reference

| Error code | Immediate diagnostic |
|------------|---------------------|
| `BINARY_NOT_FOUND` | Run `doctor`; install the CLI per `docs/integration-testing.md` |
| `AUTH_REQUIRED` | Run `doctor`; log in or set the API key env var once |
| `WORKSPACE_NOT_TRUSTED` | Pass `trust_workspace=true` or add path to `trusted_workspaces` |
| `READONLY_VIOLATED` | A `read_only`/`propose` delegation modified the git working tree; check the CLI output or disable `verify_read_only` |
| `CONTRACT_MISMATCH` | An adapter's output did not satisfy the expected contract (e.g. wrong type or missing field); check CLI version against adapter verification date |
| `TOO_MANY_JOBS` | Cap of `max_jobs` retained background jobs reached; collect and let jobs expire, or raise `max_jobs` in config |
| `TIMEOUT` | Raise `timeout_s` on the call or `default_timeout_s` in config |
| `MAX_DEPTH_EXCEEDED` | Raise `max_depth` in config (default 3); check for self-referential prompts |
| `TOO_MANY_TARGETS` | Reduce targets or raise `max_targets` (default 8) |
| `TRANSCRIPT_NOT_FOUND` | Pin `agy` version; serialize concurrent `agy` calls; check `include_raw` |
| `PARSE_ERROR` | Set `include_raw=true`; compare CLI version to adapter verification date |
| `JOB_NOT_FOUND` | Collect results before TTL (`job_ttl_s`, default 1 hour); id is a hex uuid4 string |
| `ROLE_NOT_FOUND` | Add directory to `role_dirs`; run `roles` tool to list visible roles |
| `UNKNOWN_TARGET` | Run `capabilities` to list registered adapter ids |
