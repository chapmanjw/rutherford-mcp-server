# Security model

This document expands on the top-level `SECURITY.md`. The audience is operators deploying
Rutherford and contributors touching its security-relevant code paths. For vulnerability
reporting, see `SECURITY.md`.

Rutherford spawns other coding agents as subprocesses on the host. That capability carries the
same trust requirements as a shell. The sections below explain each guard in detail: where it
lives in code, what it enforces, and how to configure it correctly.

---

## SafetyMode: the four-level ladder

Every delegation carries a `SafetyMode` from `domain/enums.py`. In ascending permission order:

| Mode        | Meaning                                                                          | Mutates workspace? |
|-------------|---------------------------------------------------------------------------------|-------------------|
| `read_only` | Inspect only; the agent must not modify files.                                  | No                |
| `propose`   | The agent may produce a diff or plan, but not apply it.                         | No                |
| `write`     | The agent may apply changes using the CLI's normal approval flow.               | Yes               |
| `yolo`      | The agent acts without approval prompts (the CLI's bypass/skip-permissions flag). | Yes              |

`read_only` is the default everywhere. `DelegationRequest.safety_mode` defaults to
`SafetyMode.READ_ONLY`; `RutherfordConfig.default_safety_mode` defaults to `SafetyMode.READ_ONLY`.
A caller must explicitly pass `write` or `yolo` to get mutating behavior.

`is_mutating()` in `domain/enums.py` classifies `write` and `yolo` as mutating and everything
else as non-mutating. `DelegationService.delegate` calls it before spawning:

```python
if is_mutating(req.safety_mode) and not self._workspace_trusted(req):
    return self._fail(ctx, ErrorCode.WORKSPACE_NOT_TRUSTED, ...)
```

### How adapters map SafetyMode

Each adapter implements `map_safety(mode: SafetyMode) -> SafetyFlags`. The returned `SafetyFlags`
carries `args` (appended to the argv) and `env` (overlaid on the child environment). Adapters
must map every mode and must default conservatively: an unknown or non-mutating mode must never
produce a bypass flag.

Three concrete examples from the code:

**claude_code** (`adapters/claude_code.py`)

| SafetyMode    | argv fragment                        | Note                                               |
|---------------|--------------------------------------|----------------------------------------------------|
| `read_only`   | _(none)_                             | Headless `-p` mode never auto-applies edits.       |
| `propose`     | _(none)_                             | Same: changes are described but not applied.       |
| `write`       | `--permission-mode acceptEdits`      | Auto-approves file edits; shell still gated.       |
| `yolo`        | `--dangerously-skip-permissions`     | Bypasses all permission checks.                    |

**codex** (`adapters/codex.py`)

| SafetyMode    | argv fragment                                   | Note                                              |
|---------------|-------------------------------------------------|---------------------------------------------------|
| `read_only`   | `-s read-only -a never`                         | Read-only sandbox; no approval prompts.           |
| `propose`     | `-s read-only -a never`                         | Same sandbox.                                     |
| `write`       | `-s workspace-write -a never`                   | Workspace-write sandbox; no approval prompts.     |
| `yolo`        | `--dangerously-bypass-approvals-and-sandbox`    | Bypasses all sandboxing and approvals.            |

The `-a never` flag on `read_only` and `write` keeps Codex headless: it never pauses waiting
for a human approval prompt that would hang the run. Rutherford enforces the approval policy
via the sandbox level, not by relying on user interaction.

**antigravity** (`adapters/antigravity.py`)

`agy -p` (print mode) has no granular approval mechanism. The adapter maps `write` and `yolo`
identically to `--dangerously-skip-permissions`; `read_only` and `propose` pass no flag, and the
agent's changes are simply not applied in print mode. This is a best-effort mapping: the
distinction between `write` (normal approval flow) and `yolo` (bypass) that other adapters offer
is absent here. Operators who need a clear approval boundary should not use Antigravity for
mutating delegations.

**goose** (`adapters/goose.py`)

Goose has no approval flag; posture is set via the `GOOSE_MODE` environment variable. The adapter
maps `read_only` and `propose` to `GOOSE_MODE=smart_approve` and `write` and `yolo` to
`GOOSE_MODE=auto`. Note: `GOOSE_MODE=auto` is reportedly ignored by the claude-code provider in
Goose (verified as a known upstream issue as of 2026-05-30); treat Goose's write/yolo behavior as
unverified when using that provider.

---

## Trusted-workspace gate

`write` and `yolo` require both an explicit `safety_mode` argument *and* a passing
trusted-workspace check. The check is in `DelegationService._workspace_trusted`
(`services/delegation.py`):

```python
def _workspace_trusted(self, req: DelegationRequest) -> bool:
    if req.trust_workspace:
        return True
    if not req.working_dir:
        return False
    target_dir = Path(req.working_dir).resolve()
    for trusted in self._config.trusted_workspaces:
        root = Path(trusted).resolve()
        if target_dir == root or target_dir.is_relative_to(root):
            return True
    return False
```

Two ways to pass the gate:

1. **Allowlist**: Add an absolute path to `trusted_workspaces` in `rutherford.toml`. Any
   `working_dir` that resolves to that path or a subdirectory of it is allowed. Paths are
   resolved with `Path.resolve()` before comparison, so symlinks and relative segments do not
   bypass the check.

2. **Per-call flag**: Pass `trust_workspace=true` in the tool call. This is an explicit,
   call-site opt-in for a directory not on the allowlist. Use it for one-off cases; rely on the
   allowlist for directories you delegate to regularly.

If neither condition is satisfied, the delegation fails immediately with
`ErrorCode.WORKSPACE_NOT_TRUSTED` and the subprocess is never spawned. A delegation that omits
`working_dir` also fails the gate, because `_workspace_trusted` returns `False` when there is no
directory to check.

---

## Argv arrays, never shell strings

`CLIAdapter.build_invocation` (`adapters/base.py`) returns an `InvocationSpec` whose `argv`
field is `list[str]`. The docstring is explicit: "Must never build a shell string." The
`AsyncProcessRunner` in `runtime/process.py` passes that list to
`asyncio.create_subprocess_exec`, which takes an argv array and never invokes a shell. There is
no point at which a prompt, path, or any other user-controlled value is interpolated into a
command string.

### Windows shim exception

Several CLIs on Windows install as `.cmd`, `.bat`, or extension-less npm shims.
`CreateProcess` cannot launch those directly (it raises `WinError 193`). `prepare_argv` in
`runtime/launch.py` detects this at spawn time and wraps them:

- `.cmd` / `.bat` / extension-less shim: `[cmd.exe, /c, <resolved>, *rest]`
- `.ps1` shim: `[pwsh, -NoProfile, -ExecutionPolicy, Bypass, -File, <resolved>, *rest]`
- `.exe` or any POSIX binary: launched directly

Arguments are always passed as separate list elements in the final argv. No command string is
concatenated. The `cmd.exe /c <path>` form passes the resolved absolute path as a single
argument; it does not assemble a shell command with the prompt or user inputs embedded in it.
`prepare_argv` is a pure function of its injected `which` and `is_windows`, so both paths are
unit-tested from a single host without platform switching.

---

## Depth guard and target cap

Rutherford is itself an MCP server that a CLI can call, creating a delegation chain. Without a
bound, a chain could recurse without limit and fan out across many targets.

Two guards in `runtime/depth.py` bound this:

**Depth guard**

`RUTHERFORD_DEPTH` is an environment variable that carries the current depth across process
boundaries. `DelegationService.delegate` injects it into every child's environment via
`child_depth_env(base_depth)`, which sets `RUTHERFORD_DEPTH = base_depth + 1`. Before
spawning, it calls `ensure_within_depth(base_depth, config.max_depth)`, which raises
`DepthLimitError` if `base_depth >= max_depth`. The child therefore runs at depth `base_depth + 1`
and its own delegations are refused at `max_depth`.

The default `max_depth` is `3` (`config/schema.py`). Depth 0 is a direct caller; depth 1 is a
subprocess of that caller; depth 2 is a subprocess of that subprocess; a delegation at depth 3
is refused. Set `max_depth` in `rutherford.toml` to adjust.

**Target cap**

`ensure_within_target_cap(count, max_targets)` refuses a consensus call that fans out to more
targets than `config.max_targets`. The default is `8`. An `ErrorCode.TOO_MANY_TARGETS` result
is returned without spawning any subprocess.

Both guards fail fast before any subprocess is started. Neither is bypassable by the spawned
process: a child reads its depth from the environment and cannot forge a lower value to escape the
guard (the guard reads `RUTHERFORD_DEPTH` from the environment Rutherford sets, not from anything
the child reports back).

---

## Auth: detect-only, no interactive logins

`CLIAdapter.check_auth` probes auth state without triggering a login. The contract from
`adapters/base.py`: "Probe auth state without ever triggering a login."

The four states in `AuthState` (`domain/enums.py`):

| State             | Meaning                                                              |
|-------------------|----------------------------------------------------------------------|
| `authenticated`   | A usable credential or session was detected.                         |
| `needs_login`     | The CLI requires an interactive login that Rutherford will not do.  |
| `api_key_missing` | The CLI expects an API-key env var and none was found.               |
| `unknown`         | Auth state cannot be determined without running the CLI interactively. |

Adapters check for an API-key env var first (by name only, reading the variable, not its value),
then run a non-interactive status command if one exists. If neither is available, they report
`unknown` rather than hang. The `doctor` tool surfaces these states to the operator; it does not
attempt a login on an unauthenticated target.

Antigravity is the special case: `agy` authenticates with a Google OAuth flow, exposes no
`whoami`, and stores its token in a location that varies by platform and install (keyring vs an
on-disk file, and a different path under WSL), so no cheap probe is trustworthy. Its `check_auth`
returns `AuthState.UNKNOWN`. The `doctor` tool resolves that by default (`live=true`) with a
minimal read-only round trip -- the only reliable signal absent a status command -- reclassifying
it to `authenticated` or `needs_login`.

---

## Secrets handling

Rutherford never handles a credential value. Specifically:

- API keys are read by name from the environment to detect auth state. The value is never read,
  printed, or passed to any subprocess. `BaseCLIAdapter._env_present(*names)` calls
  `os.environ.get(name)` and returns the name of the first non-empty variable, not its value.
- Credential values never appear in `DelegationResult`. The result envelope carries text, cost,
  artifacts, and error info -- no env-var values.
- No adapter writes a credential to disk or passes it on the argv.
- The child process inherits the parent environment via `merged_env` in `runtime/launch.py`,
  which overlays only the keys listed in `InvocationSpec.env`. API keys in the environment are
  inherited this way if they exist; they are not injected by Rutherford. The adapter's `env`
  overlay carries only safety-mode control variables (for example, `GOOSE_MODE`,
  `OPENCODE_PERMISSION`), never credentials.

Keep API keys in environment variables or each CLI's own credential store. Do not put them in
`rutherford.toml`, role files, or anywhere else in the repository.

---

## Process-tree termination

Every run has a timeout (`config.default_timeout_s`, default 300 seconds; overridable per call
via `DelegationRequest.timeout_s`). On timeout or asyncio cancellation, `AsyncProcessRunner` in
`runtime/process.py` calls `_kill_process_tree(process.pid)`:

```python
except (TimeoutError, asyncio.CancelledError) as exc:
    await asyncio.to_thread(_kill_process_tree, process.pid)
```

`_kill_process_tree` uses psutil to collect the direct child and all descendants, then sends
`terminate()` to each, waits up to 3 seconds, and sends `kill()` to any that remain. It uses
`terminate()` and `kill()` rather than raw signals so the behavior is identical on POSIX and
Windows. The result is a `ProcessResult` with `timed_out=True` and `exit_code=None`.

Killing only the direct child is not sufficient because coding agents spawn their own children
(language servers, tool processes, compilers). Without a recursive kill, those processes linger
after a timeout.

---

## Operator checklist

Before exposing Rutherford to an MCP client:

- [ ] Set `trusted_workspaces` in `rutherford.toml` to only the directories you intend to allow
  mutating delegations into. Leave it empty if you will only use `read_only` and `propose`.
- [ ] Verify auth state for every adapter you plan to use: `doctor` reports `authenticated`,
  `needs_login`, `api_key_missing`, or `unknown` per adapter. Fix any that are not
  `authenticated` before delegating to them.
- [ ] Set `max_depth` and `max_targets` in `rutherford.toml` if the defaults (3 and 8) are not
  right for your environment. Lower values are more conservative.
- [ ] Keep API keys and session tokens in environment variables or each CLI's own credential
  store. Do not put credentials in `rutherford.toml` or any file in the repository.
- [ ] If you add a generic adapter via `generic_adapters` in `rutherford.toml`, populate the
  `safety` section with explicit per-mode argv fragments for all four modes. An empty `yolo`
  entry means the generic adapter passes no bypass flag in yolo mode -- which may or may not be
  the right behavior for that CLI.
- [ ] Set `default_timeout_s` to a value appropriate for your slowest expected workload. The
  default is 300 seconds. Agents that hang (waiting for interactive input, for example) will be
  killed at this deadline.
- [ ] Review `enabled_adapters` in `rutherford.toml` to disable adapters you do not use. A
  disabled adapter cannot be targeted even if its binary is installed.

---

## Reporting a vulnerability

Report security issues through
[GitHub Security Advisories](https://github.com/chapmanjw/rutherford-mcp-server/security/advisories/new).
Do not file a public issue. You will receive an acknowledgement within a few days.
