# Security model

This document expands on the top-level `SECURITY.md`. The audience is operators deploying Rutherford
and contributors touching its security-relevant code paths. For vulnerability reporting, see
`SECURITY.md`.

Rutherford spawns other coding agents as subprocesses on the host and acts as the permission authority
for what those agents do over ACP. That capability carries the same trust requirements as a shell. The
sections below explain each guard: where it lives in code, what it enforces, and how to configure it.

---

## SafetyMode: the four-level ladder

Every delegation carries a `SafetyMode` from `domain/enums.py`. In ascending permission order:

| Mode | Meaning | Mutates workspace? |
| --- | --- | --- |
| `read_only` | Inspect only; the agent must not modify files. | No |
| `propose` | The agent may describe a change but not apply it. | No |
| `write` | The agent may apply changes, subject to its own approvals. | Yes |
| `yolo` | The agent acts without approval prompts. | Yes |

`read_only` is the default out of the box. `DelegationRequest.safety_mode` defaults to
`SafetyMode.READ_ONLY`, and `RutherfordConfig.default_safety_mode` defaults to it too. A `delegate` /
`consensus` / `debate` call that omits `safety_mode` adopts the configured default (an explicit value
always wins). Configuring a mutating default does not bypass anything: `write` / `yolo` still require a
trusted workspace, however the mode arrived.

---

## The permission engine: how a mode becomes ACP decisions

Under ACP, Rutherford is the client that answers the agent's permission, filesystem, and terminal
requests as the turn runs. `acp/permission.py:PermissionPolicy` renders the safety mode into those
decisions, and `acp/client.py` applies them as the agent calls back:

| Request from the agent | `read_only` / `propose` | `write` / `yolo` |
| --- | --- | --- |
| filesystem read | served | served |
| filesystem write | denied | allowed |
| terminal execution | denied | allowed |
| tool-call permission | rejected (decline the tool, not the turn) | allowed (one-shot `_once` form preferred) |

A read is always served — the answer needs to see the code. For a non-mutating mode, a write or
terminal request is denied and a tool-call permission request is answered with the agent's `reject_*`
option, so the agent's own loop continues without the side effect rather than the whole turn being
cancelled. This is the structured ACP equivalent of the v2 per-CLI safety flags: the policy is
enforced by Rutherford at each request, not by passing a CLI a `--read-only` flag and trusting it.

The permission engine governs what the agent routes through ACP. An OS-level sandbox (worktree
isolation) is a later layer, so the optional `verify_read_only` git check (below) still belongs above
this for defense in depth.

---

## Trusted-workspace gate

`write` and `yolo` require both an explicit (or configured) mutating mode *and* a passing
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

1. **Allowlist.** Add an absolute path to `trusted_workspaces` in config. Any `working_dir` that
   resolves to that path or a subdirectory of it is allowed. Paths are resolved with `Path.resolve()`
   before comparison, so symlinks and relative segments do not bypass the check.
2. **Per-call flag.** Pass `trust_workspace=true` in the tool call — an explicit, call-site opt-in for
   a directory not on the allowlist.

If neither holds, the delegation fails immediately with `WORKSPACE_NOT_TRUSTED` and no agent is
spawned. A delegation that omits `working_dir` also fails the gate, because there is no directory to
check.

---

## Launch resolution: clean stdio, no shell string

Rutherford spawns agents with `acp.spawn_agent_process`, which uses an argv array and never a shell.
The launch argv is resolved by `acp/launch.py:prepare_argv`. Its real job is correctness on Windows,
where an npm shim cannot be launched directly and a `cmd /c` or PowerShell wrapper would corrupt the
raw JSON-RPC stdin the ACP transport needs.

- An npm `.cmd` / `.ps1` shim is resolved to its real target — the bundled `.exe`, or `node <entry>.js`
  — and launched directly with clean stdio.
- A non-npm `.cmd` / `.bat` / `.ps1` shim falls back to the `.ps1` sibling via PowerShell, then
  `cmd /c`.
- A `.exe` or any POSIX binary is launched directly.

Arguments are always separate list elements; no command string is assembled, and no prompt, path, or
other input is interpolated into a command line. `prepare_argv` is a pure function of its inputs, so
both paths are unit-tested from a single host.

---

## Config is trusted as code

Project-scoped config (`.rutherford/config.toml` and a discovered `.rutherford/acp.json`) can set an
agent's launch `command` and subprocess `env`. The loader keys discovery off the process working
directory. Treat starting the server in a directory the same way you treat running a shell there: only
start Rutherford in a workspace you trust. An imported `acp.json` that collides with a built-in id is
skipped, so an auto-import can never silently replace a curated built-in launch.

---

## Auth: reuse, never log in

Rutherford never performs an interactive login. Each agent reaches its model with its own existing
login or API key, in the agent's own account. There is no cheap, trustworthy non-interactive auth
probe for an ACP agent — so the health signal is a real round trip. `doctor` drives each agent with a
trivial read-only ACP turn and reports `ok`, `no_answer`, `handshake_failed`, `not_installed`, or
`error`. `capabilities` is the cheap snapshot of the registry; it does not call any agent.

`codex` (`codex-acp`) and `claude_code` (`claude-agent-acp`) drive their CLI over ACP using the
existing CLI login, with no API key. Other agents use whatever auth their own login established.

---

## Secrets handling

Rutherford does not handle a credential value. The agent subprocess inherits the environment so its
own credential discovery works; Rutherford layers only the descriptor's `env_overrides` on top (a
local-runtime provider env, never a credential it minted). A credential value never appears in a
`DelegationResult`, which carries text, cost, provenance, and error info only. Keep API keys and
session tokens in environment variables or each agent's own credential store. Do not put them in a
config file, a role file, or anywhere else in the repository.

---

## Process-tree teardown

Every turn has a timeout (`default_timeout_s`, default 300s; overridable per call via `timeout_s`). On
timeout the session issues `session/cancel` and the turn fails as `ACP_TURN_TIMEOUT`, preserving any
streamed partial answer on the result. When a session closes, `acp/teardown.py` reaps the agent's
orphaned descendant process tree: a wrapper agent spawns the underlying CLI as a child, and the SDK
transport terminates only the direct child, so the descendants are snapshotted before teardown (a dead
parent's children reparent and drop out of the walk) and killed after. This keeps a timed-out or
cancelled agent's forked CLI from lingering and holding the working directory.

---

## Optional read-only verification

`verify_read_only` (off by default) turns the read-only promise into a checked invariant. After a
successful `read_only` or `propose` delegation whose `working_dir` is a git repo, Rutherford
fingerprints the tree under `working_dir` before and after the run and fails the result with
`READONLY_VIOLATED` if it changed. It catches a further edit to an already-dirty file and a write to a
gitignored path. Limits: a write *outside* the repo is unobservable, and under concurrent fan-out on a
*shared* tree a peer's write can be mis-attributed — it is soundest for a single delegation. It adds
git calls per delegation, hence off by default.

---

## Operator checklist

Before exposing Rutherford to an MCP client:

- [ ] Set `trusted_workspaces` to only the directories you intend to allow mutating delegations into.
  Leave it empty if you will only use `read_only` and `propose`.
- [ ] Run `doctor` and confirm the agents you plan to use report `ok`. Fix any that do not before
  delegating to them.
- [ ] Start the server only in a working directory you trust — project config can set launch commands.
- [ ] Set `max_depth` and `max_targets` if the defaults (3 and 8) are not right for your environment.
- [ ] Keep API keys and session tokens in environment variables or each agent's own credential store,
  never in a repo file.
- [ ] Set `default_timeout_s` to suit your slowest expected workload (default 300s).
- [ ] Use `enabled_agents` to restrict the registry to the agents you actually use.

---

## Reporting a vulnerability

Report security issues through
[GitHub Security Advisories](https://github.com/chapmanjw/rutherford-mcp-server/security/advisories/new).
Do not file a public issue. You will receive an acknowledgement within a few days.
