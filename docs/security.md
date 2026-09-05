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

The permission engine governs what the agent routes through ACP. For a *mutating* mode it is paired with
the **write sandbox** (below), which runs the agent in an isolated execution root rather than the user's
tree; the optional `verify_read_only` git check (below) is the defense-in-depth backstop for the
`read_only` path, which runs directly in `working_dir`.

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
   before comparison, so symlinks and relative segments do not bypass the check. From a repo root,
   register the current directory in the **global** allowlist with:

   ```sh
   rutherford-mcp-server trust           # or: python -m rutherford trust
   rutherford-mcp-server trust --list    # show the global allowlist
   rutherford-mcp-server untrust         # remove cwd from the global allowlist
   ```

   `trust` / `untrust` edit only the platform global `config.toml`
   (`%APPDATA%\rutherford\config.toml` on Windows, `$XDG_CONFIG_HOME/rutherford/config.toml` elsewhere).
   They create the file when missing, never clobber unrelated keys, and refuse a malformed TOML. A
   project-local `trusted_workspaces` still *replaces* the global list at load time (it does not union).
   Config is read once at server start, so restart or reconnect the server before retrying a delegation.

2. **Per-call flag.** Pass `trust_workspace=true` in the tool call — an explicit, call-site opt-in for
   a directory not on the allowlist.

If neither holds, the delegation fails immediately with `WORKSPACE_NOT_TRUSTED` and no agent is
spawned. A delegation that omits `working_dir` also fails the gate, because there is no directory to
check. A sandboxed mode (`propose` / `write` / `yolo`) with no `working_dir` is refused outright — there
is no tree to isolate, and the turn must never fall through to running in the server's own directory.

---

## The write sandbox

`delegate` is the **single write path**. A mutating (`propose` / `write` / `yolo`) delegation runs in an
isolated execution root built by `acp/sandbox.py`; only a reviewed diff is ever applied back. (The panels —
`consensus`, `debate`, `review`, `plan` — are read-only deliberation and refuse a mutating mode at the
service boundary: there is no coherent merge of edits from several agents into one tree.)

Out of the box an agent can never edit the real tree directly. An operator can deliberately turn that
guarantee off for a named directory — see [Direct workspace mutation](#direct-workspace-mutation-the-opt-out)
below, which is off by default and cannot be enabled by a caller.

The execution root is chosen by what `working_dir` is:

- **A git repo** → an ephemeral detached worktree off `HEAD` (`git worktree add --detach`). The agent's
  spawn cwd, the ACP `session/new` cwd, and the file/terminal confinement root are all the worktree. After
  the turn the changed set is computed from the worktree (`git diff --cached --binary --no-renames` plus
  the name-status). `propose` discards the worktree; `write` / `yolo` copy the changed files back
  byte-for-byte (a copy, not `git apply`, so Windows `core.autocrlf` cannot inject `\r`) and remove the
  deleted ones.
- **An existing non-git directory** → a bounded temporary copy (a size guard refuses a huge tree, pointing
  the caller at git; symlinks are skipped, never dereferenced). The agent edits the copy; the changes are
  diffed against an open-time per-file-hash baseline (so only the agent's edits count) and applied back.
- **A fresh path that does not exist yet** → producing into a brand-new, non-git location (scaffold a
  project, write a report). The sandbox is an empty directory and `write` / `yolo` create the real
  directory as they write the produced files; `propose` applies nothing, so the path stays absent. This is
  a first-class "write / produce things that are not in a git repo" path.

Guards on the apply-back (each with a test in `tests/test_sandbox.py`):

- **Path containment.** A changed file is written only if `working_dir/<rel>` resolves *inside* the
  resolved `working_dir`; a destination symlink is replaced at its own location, never written *through*
  (so it can't redirect the write to another file). A delete resolves only the parent, removing a symlink
  as the link itself.
- **No silent clobber.** A git apply refuses if a file it would touch has an *uncommitted* edit vs `HEAD`
  in the real tree (the worktree is off `HEAD`, so applying back would overwrite that local work); the
  check runs under the repo's own `autocrlf`. The non-git apply refuses if a touched file was edited in
  the real tree *during* the turn (a concurrent edit).
- **Always cleaned up.** The worktree / temp copy is removed in a `finally` (and on a cancellation mid-open
  via a shielded open), and the agent's process tree is reaped on session close.

Two limitations are deliberate, given the threat model (orchestrating *cooperative* coding agents the user
chose to run, not sandboxing adversarial code):

- **Not an OS jail.** The isolation is cwd + the ACP path-escape guard. A `write` / `yolo` agent's own OS
  process, or a terminal command it runs, can still write an absolute path outside the sandbox. Full OS
  containment (Job Objects / ACLs) is deferred. This is strictly safer than v2, which ran agents directly
  in the user's tree with no sandbox.
- **A narrow apply-time TOCTOU.** A user save in the sub-millisecond window between the clobber check and
  the copy is not detected — the same gap any check-then-write filesystem apply (`git apply` / `git stash`)
  has. Eliminating it would require locking the whole working tree for the apply.

### Direct workspace mutation (the opt-out)

Some workflows need an agent to act in the live tree — install dependencies, run local tooling, and see
the side effects in place — where handing back a diff is the wrong shape. `direct_workspace_mutation=true`
on `delegate` asks for that: a `write` / `yolo` agent whose cwd, file root, and terminal all point at the
real `working_dir`.

It forfeits everything the section above describes. No worktree or temp copy, no clobber check, no
concurrent-edit check, no containment on apply-back (there is no apply-back), no committed-`HEAD` starting
point, and **no diff** — so the run leaves no record of what it changed, and the durable job record has no
changed-file list. A failed, timed-out, or cancelled run may have written a partial change with nothing to
say what.

Because of that, asking is never sufficient. Every one of these must hold. The caller chooses the mode, names
the directory, and asks for the capability; what it cannot do is grant itself the two conditions the operator
decides:

| Condition | Who decides |
|---|---|
| `allow_direct_workspace_mutation = true` in `config.toml` | the operator, out of band, restart required |
| `working_dir` is on the configured `trusted_workspaces` allowlist | the operator — a per-call `trust_workspace=true` does **not** qualify |
| `working_dir` given explicitly | the caller; there is no fallback to the server's own directory |
| not nested inside another delegation | inherited — a Rutherford running inside an agent reads `RUTHERFORD_DEPTH` from its parent and counts as nested. Defence in depth, **not** a boundary; see below |
| mode is `write` / `yolo` | `propose` is refused (its diff *is* the sandbox); `read_only` already runs in place |

The separation between the config allowlist and the per-call `trust_workspace` is the load-bearing part.
`trust_workspace` is supplied by whoever makes the call, so a model driving `delegate` can set it for
itself; if it also opened this gate, the caller could authorise its own unsandboxed writes and the operator's
allowlist would decide nothing.

Each admitted run logs `direct_workspace_mutation_admitted` *before* launch and
`direct_workspace_mutation_finished` after, and its result carries `direct_mutation=true` so a reader can
tell the missing diff means "never captured" rather than "nothing was written".

The record is **best effort, not a guarantee**, and it is worth being precise about why. Rutherford's
stderr logger hands records to a daemon thread behind a bounded queue, so that a host which never drains
its pipe cannot freeze the event loop. That means an admission record can be dropped under log saturation,
lost if the writer thread dies on a stream error, or discarded at process exit because nothing joins a
daemon thread. The record is *attempted* before the agent starts, so a run that hangs has usually left it and
a run killed in the wrong millisecond may not — but "usually" is the honest word here, and nothing in this
process upgrades it to a guarantee.

Gating the run on a *confirmed* write — refusing to launch unless the sink acknowledged the record — was
built and then removed. It is recorded here because the reasoning outlives the code. Confirming a write to
a sink that may be wedged, without blocking indefinitely, requires the caller to take over queue admission,
cancellation, per-handler durability and the failures `logging` deliberately absorbs. Each attempt at it
introduced a fresh concurrency defect, and the last one still could not withdraw a write already in flight:
a refused run could leave an "admitted" record behind it, which is worse than no record, because an audit
trail that invents an event is one a reader will act on. The machinery also blocked the event loop for
roughly half a second per admitted run, which a caller could use to degrade the whole server. It cost more
than it bought.

**If you need a trail that survives, do not rely on this log line.** Collect the server's stderr, or enable
persistence so the run writes a durable record under its run directory. Both live outside this process,
which is the only place a real guarantee can come from.

Those log lines go through the same structured logger as everything else, which `log_format = "off"` silences
entirely — and the durable record is written only when a run persists, which is off by default. Enabled
together, the ordinary case would be an agent given write and shell access to a real tree with nothing
recording that it ran, so **`allow_direct_workspace_mutation = true` with `log_format = "off"` is rejected as
a configuration error** rather than warned about. A warning would go to the channel that was just turned off.

Both audit lines are emitted at ERROR rather than WARNING. Not because they report a failure — it is the
level that survives every `log_level` an operator can set, so the record cannot be filtered away by a
setting that looks unrelated to this capability.

A run that also persists carries `direct_mutation` in its durable record, which is worth having on a machine
that matters: the log is a stream someone has to be collecting, the record is on disk.

Three limits are worth stating rather than leaving to be discovered.

The gate resolves `working_dir` once and the run launches against that same resolved path, so the directory
that was approved and the one that is written to cannot differ *by name* — but the pinned value is still a
pathname, and a directory or ancestor swapped for a symlink between the check and the spawn would be
followed; closing that needs an identity check against an open handle, which Rutherford does not do.

The nesting condition is **not tamper-proof, and is not claimed to be**. Depth crosses the process boundary
in `RUTHERFORD_DEPTH`, which the parent sets on the child it spawns. A value that is present but unreadable
— malformed, or negative — is now fatal rather than silently read as zero, so a corrupted environment
refuses instead of quietly promoting a nested run to top level. But *absent* is indistinguishable from a
genuine top-level start, because they are the same observation, and anything that can spawn a nested
Rutherford controls that child's environment. A `write` / `yolo` agent has shell access and can therefore
launch one with the variable stripped. Treat this condition as defence against an accident and as one layer
of several, never as the layer that stops a hostile agent — and note that such an agent gains nothing it did
not already have, per the next paragraph.

The isolation forfeited here was never an OS jail to begin with (see the two deliberate limitations above):
a `write` / `yolo` agent's own process can write outside the directory regardless. What direct mutation
removes is the recoverability — the guards, the diff, and the ability to see afterwards what changed.

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

Rutherford never obtains or mints a credential of its own. The agent subprocess inherits the
environment so its own credential discovery works, and Rutherford layers the descriptor's
`env_overrides` on top of it.

That layer is the one place it will hand a credential onward, and it does so verbatim without
recognising it as one: an `[agents.<id>.env]` block is copied straight into the child's environment.
The intended use is a non-secret pin such as a provider model id. Keep API keys and session tokens in
your environment or each agent's own credential store, and out of a config file, a role file, or
anywhere else in the repository -- a value placed there is one Rutherford will copy, and one that sits
in plain text wherever that file lives.

All of that describes what Rutherford does with a credential on the way IN. It is not a guarantee
about what comes back out: a result can contain one by a separate route, which belongs to the agent
rather than to Rutherford. The two halves are not quotable apart.

Note also where such a result can come to rest. A persisted run (`persist=true`, or a `job` default)
writes each voice's answer or error to `artifacts/voices/` under the jobs directory, so anything the
masking below did not recognize is written to disk rather than only returned to the caller. Treat the
jobs directory with the same care as any other diagnostic output.

When a session fails to open, a bounded excerpt of the agent's own stderr is attached to the failure
detail -- the FIRST bytes it wrote, not the last, because a launcher that rejects its arguments
explains itself immediately, then exits, and says it nowhere else. The subprocess inherits a
credential-bearing environment, so a misconfigured adapter, proxy, or SDK that prints a token on the
way out would put that token in front of Rutherford.

Captured stderr is therefore masked for known credential shapes before it is surfaced — authorization
headers, `*_KEY` / `*_TOKEN` / `*_SECRET` assignments, vendor-prefixed keys (`sk-`, `ghp_`, `AKIA`,
`xox*`, `AIza`, `glpat-`), JWTs, presigned-URL signature parameters, armored private-key blocks
(PEM including encrypted traditional-format keys, PGP, and RFC 4716 SSH2), and credentials embedded
in a URL (`https://user:pass@host`) — and the masking runs after escape
stripping, so a sequence spliced into a token cannot evade it.

The key masking is marker-based, so it reaches armored blocks only. A non-armored private key —
a PuTTY `.ppk`, raw DER bytes, or key material printed with no markers at all — has no shape to
match on and is not covered. Read the list above as what it says rather than as "all private keys".

That last shape is worth calling out, because it is the likeliest of these to occur in practice: git,
npm, pip and curl all echo a URL back on an auth failure, so an agent that shells out to git prints
one directly. The host and user survive the masking; only the password is dropped, because which host
rejected the login is the diagnostic.

Treat that as a mitigation, not a guarantee. It matches shapes, so an unrecognized credential format
survives it; the pass is deliberately conservative because an entropy heuristic would redact the
hashes, paths, and model ids that make the diagnostic worth having. The durable controls are that the
capture is head-bounded and byte-capped, that it appears only on a failed session open and never on a
successful turn, and that it is diagnostic output rather than a log sink. An operator who cannot
accept a residual disclosure risk in error details should not run an agent whose environment carries
credentials it is willing to print.

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
