# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Local-model support for **opencode** on both Ollama and LM Studio (`[agents.<id>] base="opencode"
  backend="ollama"|"lmstudio" model=...`). opencode is configured entirely through one inline-JSON
  environment variable (`OPENCODE_CONFIG_CONTENT`) that declares an `@ai-sdk/openai-compatible` provider
  pointed at the runtime's `/v1` endpoint, so there is no config file on disk — one source of truth in
  `roster._opencode_openai`. Vetted live (2026-06-14): a real ACP turn answered on Ollama (`qwen3:8b`) and
  LM Studio (`openai/gpt-oss-20b`). This supersedes the earlier "opencode's acp turn returns empty" finding,
  which was an unconfigured-provider artifact, not an opencode limitation.
- Documented the full, honestly-vetted local-backend support matrix in `docs/local-models.md`: which
  agent × {ollama, lmstudio} pairs work, and — for the ones that do **not** — the concrete reason. `codex`
  has no local pair (its custom providers now require the OpenAI Responses API wire that local runtimes don't
  speak, and `codex-acp` is auth-gated); `hermes` can talk to Ollama but only via its own `config.yaml`
  provider (its `acp` mode ignores the inference-provider env), so it is a config-file change, not an
  env-keyed `backend`. `claude_code` on Ollama works but is slow and needs a generous timeout + a capable
  model. A new `-m integration` suite (`tests/integration/test_local_backends.py`) drives every supported
  pair live and skips a runtime that is down.
- Durable runs (F2): a `delegate` / `consensus` / `debate` call can now be kept as a job on disk. Each of
  the three tools takes a `persist` flag (`true` / `false`, or `None` to follow the configured
  `default_persistence` — `ephemeral` out of the box, so nothing is written unless asked), and the
  previously-inert `default_persistence` / `jobs_dir` config is now wired. A persisted run is written under
  `<jobs_dir>/<run_id>/` (`jobs_dir` defaults to `<cwd>/.rutherford/jobs`):
  - `state.toon` — a versioned, replay-complete `RunRecord` (TOON-encoded through the `io/serialize` seam):
    the resolved launch `argv`, requested-vs-resolved model, provenance, safety mode, requested/applied
    effort, topology, `cwd`, prompt, role, files, ok / error code, changed files, cost, stop reason, and a
    rollup. The child process `env` is **never** persisted (it can carry secrets); replay recomposes it.
  - `artifacts/answer.md` (the answer / synthesis) and, for a write run, `artifacts/diff.md` (the sandbox
    diff, including created / untracked files).
  - A persisted `consensus` writes a parent record linking a child record per voice (`child_run_ids`), with
    one `artifacts/voices/voice-N.md` per voice and a `voices/skipped.md` for an auto-panel's left-out agents;
    the parent rolls up status / cost / changed-file union and carries the resolved `PanelInputs` (roster +
    per-seat stance + session handle, strategy, synthesize, judge). A persisted `debate` writes a parent
    record plus the full `artifacts/transcript.md` (a debate drives its turns over persistent sessions, so the
    transcript carries the run rather than per-turn child records).
  - `io/ledger.py` (`RunLedger`) is the one writer of the jobs directory; persistence is best-effort — a write
    failure logs and degrades to an unpersisted result, never failing a run that already produced an answer.
  - Known limitation (carried from v2): python-toon 0.1.x cannot round-trip an inline array whose elements are
    quoted (a colon-bearing `argv` element such as a Windows path or `gemma3:12b`), so a record with such an
    `argv` is content-complete on disk but not yet machine-re-readable via `decode`. The clean-input fields
    (prompt, model, cwd, a colon-free `argv`) round-trip; an `xfail` tracks the codec fix.
- The write/propose sandbox substrate, so Rutherford can safely delegate file-writing work to an agent over
  ACP. A mutating delegation (`write` / `propose` / `yolo`) with a `working_dir` no longer runs the agent in
  the user's tree — it runs in an isolated execution root and only a reviewed diff is ever applied back.
  - SandboxManager (`acp/sandbox.py`). For a git `working_dir`, a mutating turn runs in an ephemeral detached
    git worktree off the repo's current `HEAD` (the agent's spawn cwd, the ACP `session/new` cwd, and the
    file/terminal confinement root are all the worktree). After the turn the changed set is computed
    (`git diff --cached --binary` for the patch plus `--name-status` for the created / edited / deleted
    paths): `propose` discards the worktree and returns the diff with nothing applied; `write` / `yolo` apply
    the changes back to the real `working_dir` by copying the changed files byte-for-byte (and removing the
    deleted ones) — copying rather than `git apply` so the result is byte-faithful on Windows (no `core.autocrlf`
    `\r` injection). A non-git `working_dir` runs in a bounded temp copy and copies the changed files back; a
    tree over the copy-size guard is refused with a clear "write mode needs a git working_dir" error rather than
    run unsandboxed. The worktree / copy is always cleaned up and the agent's process tree reaped.
  - FileGateway (`acp/client.py`). In a sandbox, `write_text_file` (always) and `read_text_file` (in a
    mutating sandbox) are confined to the sandbox root: a path that escapes it (`..`, an absolute path outside,
    a symlink out) is rejected with a clean `RequestError` and journaled (`fs_write_denied` / `fs_read_denied`),
    so a sandboxed agent cannot reach the rest of the disk through the ACP file callbacks.
  - TerminalBroker (`acp/client.py`). `write` / `yolo` implement the ACP terminal callbacks for real — the
    command runs with its cwd pinned to the sandbox root (a sanitized argv, no shell), with a bounded timeout
    and an output-size cap, and its process tree is reaped on kill / release / session close. `read_only` and
    `propose` keep denying terminal execution (propose may edit the throwaway worktree to produce a diff but
    must not run commands).
  - `verify_read_only` is now wired (gap 13). When the config flag is on and a successful `read_only`
    delegation ran in a git `working_dir`, the tree under it is fingerprinted (status + the staged and unstaged
    diffs, scoped to that subtree) before and after the turn; a change fails the result with
    `READONLY_VIOLATED` (`SIDE_EFFECTED`), catching an agent that touched the disk out of band.
  - The `DelegationResult` now carries `diff` (the sandboxed run's patch — for `propose`, the deliverable)
    and `changes_applied` (`True` for an applied `write` / `yolo`, `False` for a `propose`), and
    `changed_files` is now sourced from the sandbox (the per-delegation delta off `HEAD`, sound rather than
    the current dirty state).
- Named panels, the `review` / `plan` tools, and config-scope role layering, ported from v2 onto the
  ACP-native core.
  - Saved panels. A `panels.toon` file defines named, reusable rosters
    (`Panel{name, description, strategy, targets:[{cli, model, role, label, weight, parity, stance}]}`),
    discovered across the same scopes as the rest of the config — `~/.rutherford/panels.toon`, the project
    `.rutherford/panels.toon`, then `$RUTHERFORD_CONFIG_DIR` — and merged by name, the closest scope winning.
    The store loads lazily and is cached on the `AppContext`; every discovered file is validated in one pass
    against the live agent registry, so a panel naming an unknown agent, a bad strategy, an empty target list,
    or a negative weight raises `PANEL_INVALID` reporting every problem at once. `consensus` and `debate` gain
    a `panel` parameter (plus `panel_overrides`, a shallow merge re-validated through the same model): the
    saved seats and the panel's aggregation `strategy` replace explicit `targets`, and naming both is rejected
    (`targets` / `stances` are mutually exclusive with `panel`). An unknown panel is `PANEL_NOT_FOUND`. A new
    `reload_panels` tool re-reads the files and returns `{reloaded, count, panels:[{name, description,
    target_count}]}`.
  - The `review` tool. A read-only `consensus` over a code diff or a set of files under the
    `principal-reviewer` persona, synthesize on by default. `review(targets|panel, paths=None, diff=None,
    role="principal-reviewer", synthesize=True, ...)` builds the review prompt from the inlined `diff` or the
    in-scope `paths`, runs every voice read-only (the tool takes no `safety_mode`, so an inspection-named tool
    can never mutate), and returns the consensus envelope.
  - The `plan` tool. A read-only `delegate` under the `architect` persona: `plan(cli, goal, model=None,
    working_dir=None, files=None, timeout_s=None)` asks one agent to design an approach (not implement it),
    clamped to read-only, returning the delegation envelope.
  - Role-scope layering. The role store now layers custom roles across the v2 scope order — built-in (package)
    → each `config.role_dirs` → `~/.rutherford/roles/` → the project `./.rutherford/roles/` →
    `$RUTHERFORD_CONFIG_DIR` — the closest scope winning by id, so a workspace can override a built-in. Role
    files may be markdown (the body is the prompt) or TOON (a `prompt` / `system_prompt` field). Each `Role`
    records its `source` (`built-in` | a `role_dirs` path | `user` | `project` | `env`), surfaced in
    `list_roles`. Loading stays tolerant: a malformed or unreadable role file is logged and skipped, never a
    startup crash.
- The reliability layer over ACP — ReexecutionSafety-gated fallback and per-agent cooldown / quarantine
  (F7), ported from v2.
  - Cross-target + model fallback. `delegate` gains a `fallback` parameter (an ordered list of `cli` /
    `cli:model` alternates) and `allow_model_fallback` (on by default). On a FAILED turn whose
    `error.reexecution_safety is SAFE` — only a pre-prompt spawn / handshake failure that never ran the
    prompt, so it could not have spent cost or caused a side effect — Rutherford retries: first the SAME agent
    on its configured `fallback_model` when the failure looks model-unavailable (most ACP agents declare none,
    so this is a clean no-op), then each `fallback` alternate in turn until one answers. A DUPLICATE_COST /
    AMBIGUOUS / SIDE_EFFECTED failure (a refusal, an empty answer, a timeout, a mid-turn transport drop) is
    NEVER retried, and a write / yolo delegation never falls back (a partial mutation may have happened). The
    result records `fallback_from` (the originally requested model), `fallback_chain` (the labels that failed
    before the one that answered, a benched alternate shown as `<label> (benched)`), and bumps
    `delegation_call_count` per attempt — which already feeds `Topology.realized_delegations`. The consensus /
    debate voices benefit through the same primitive (fallback is opt-in per call).
  - Cooldown / quarantine. A new `acp/cooldown.py` `CooldownTracker` (keyed per agent id, in-memory,
    process-global on the `AppContext`) benches an agent after `cooldown_threshold` UNHEALTHY ACP failures
    within `cooldown_window_s`, for `cooldown_duration_s` (`0` disables). A new `acp/failures.py` classifies
    UNHEALTHY (the seat is broken: `ACP_SPAWN_FAILED` / `ACP_HANDSHAKE_FAILED` / `ACP_TURN_TIMEOUT` /
    `ACP_TURN_ERROR` and the rate-limit / auth classes) vs CLEAN (the request's fault: a refusal, an empty
    answer, a bad-prompt guard — these never bench a healthy agent). A benched agent is left OUT of an
    `expand_all` auto-panel (recorded in `skipped` as `benched, Ns remaining`) and skipped as a fallback
    candidate, but an EXPLICIT `delegate` to it STILL RUNS — cooldown shapes auto-selection, it never blocks a
    direct request. The `cooldown_threshold` / `cooldown_window_s` / `cooldown_duration_s` config fields move
    out of the "not yet wired" list in `docs/configuration.md`. An optional per-agent `fallback_model` config
    field (and `AgentDescriptor` field) lets an agent that can decline a model recover on a known-good one; no
    built-in agent declares one.

- N1 topology, a per-voice live-activity stream, sync progress push, and the cross-cutting limits over ACP
  (item 3 + reliability), ported from v2.
  - Topology. `consensus` / `StrategyResult` / `debate` and a single `delegate` result now carry a populated
    `Topology`: `declared` (the intended width), `realized_delegations` (Rutherford's own ACP delegations
    summed across voices/turns, incl. any fallback re-runs), `observed_peak_agents` (a FLOOR sampled from the
    live process tree — a coarse psutil timer walks each agent's descendants during a turn, reusing
    `teardown`, and the panel takes the peak; a CLI's remote agents are invisible, hence "floor"), and
    `over_cap`.
  - Per-voice activity stream + the `activity` tool. The services emit one `ActivityEvent` stream from a
    single source — a panel emits `panel_started`, a `voice_started` / `voice_finished` (or `cut`) per voice
    under a stable correlation id, and `panel_finished`, guaranteed exactly one terminal event by a
    `PanelLifecycle` wrapper. A background job buffers the stream on a bounded `JobRecord.activity[]`, and the
    `activity` MCP tool now returns one row PER VOICE in flight (`{job_id, tool, cli, model, role, status,
    elapsed_s, observed_agents, budget_left_s}`) instead of one row per job, still `{active, count}`.
  - Sync progress push. A synchronous `consensus` / `debate` call pushes MCP progress notifications as voices
    finish (a consensus-fraction `done`/`declared`), via a `make_progress_pusher` -> `Context.report_progress`
    seam in `server.py`, gated on a client-supplied `progressToken` (silent otherwise). An async job is polled
    via `activity` instead.
  - Limits. A `max_concurrency` semaphore (held around every ACP turn by the delegation primitive and shared
    with the consensus budget-harvest and a debate's round turns) bounds how many live ACP sessions a wide
    panel launches at once. The aggregate-agent cap is wired: a panel whose declared width exceeds
    `max_agents_advisory` flags `Topology.over_cap` and logs a warning, or — with `enforce_agent_cap=true` —
    is refused up front with `AGENT_CAP_EXCEEDED`. A new `runtime/depth.py` propagates `RUTHERFORD_DEPTH`
    (+ count-first `RUTHERFORD_LINEAGE` and `RUTHERFORD_PARENT_RUN`) into every spawned ACP agent's
    environment and enforces `max_depth`, refusing a too-deep Rutherford-driving-Rutherford call with
    `MAX_DEPTH_EXCEEDED`. The `max_concurrency`, `max_agents_advisory`, `enforce_agent_cap`, and `max_depth`
    config fields move out of the "not yet wired" list in `docs/configuration.md`.

- Reasoning-effort tiers and a whole-panel time budget with harvest over ACP (F8a), ported from v2.
  - Effort tiers. `delegate` / `consensus` / `debate` gain an `effort` (low | medium | high | xhigh)
    parameter, resolved per call as explicit `effort` -> per-agent `[agents.<id>] effort` -> global
    `default_effort` -> none. Since ACP has no effort field, each tier maps to an agent's real knob through a
    new `acp/effort.py`: **codex** encodes it in the ACP model id as `model[effort]` (the `codex-acp` adapter
    parses the bracket; `xhigh` supported), **cursor** encodes it as a `model-<tier>` suffix (clamps `xhigh`
    to `high`), **cline** passes the global `--thinking <tier>` launch flag, and **junie** sets the
    `JUNIE_EFFORT` env (best-effort: documented as a new-session default, ACP-mode application unconfirmed).
    Every other agent — including **pi**, whose `--thinking` is an in-session selector with no launch knob —
    is an honest reported no-op. The codex/cursor model rewrite reaches the agent via a best-effort
    `session/set_model` (sent only for a model the agent advertised). The result carries `effort` (requested)
    and `effort_applied` (the tier after clamping, or `None` for a no-op).
  - Time budget + harvest. `consensus` and `debate` gain `time_budget_s` (a wall-clock deadline for the WHOLE
    panel, distinct from each turn's `timeout_s`) and `on_budget` (harvest | continue | resume, default
    `default_on_budget`). Consensus races the voices under an `asyncio.wait` deadline: at the deadline the
    answered voices are kept and the in-flight ones are cut (their streamed partial harvested and promoted to
    a usable answer), then the panel aggregates over the harvest as long as `min_quorum` usable voices remain.
    Debate enforces the budget at round boundaries: a round still in flight at the deadline is cut (its turns
    recorded as `BUDGET_EXHAUSTED` positions with the partial preserved but never promoted to a stance) and
    the transcript so far is finalized. A harvest is a SUCCESS — `stop_reason="budget"` plus a `RunRollup`
    (issued / answered / cut / usable / quorum_met / elapsed_s / time_budget_s / effort_requested /
    effort_applied / cost); a harvest below `min_quorum` is the one genuine failure, `BUDGET_EXHAUSTED` (not
    retryable). `on_budget="continue"` makes the budget advisory (every voice / round runs to completion). A
    run that finishes within its budget sets `stop_reason=None` and a rollup with `stop_reason="ok"`.

- Consensus aggregation over ACP, ported from v2. `consensus` regains the capabilities the v3 fan-out had
  dropped, all behaviorally equivalent to v2 and computed as pure post-processing over the voices the ACP
  turns already return:
  - Strategies. A `strategy` other than `all-voices` (`unanimous` | `majority` | `plurality` | `weighted`
    | `parity-pair`) asks each voice for a verdict and reduces the panel to one outcome
    (`StrategyResult`), instead of returning every voice. `majority` / `weighted` require a *true* >50%
    share of all eligible voices (a failed or unparseable voice stays in the denominator, so an outcome
    cannot be certified off the one voice that answered); `plurality` is the lenient top-scorer rule (a
    top tie is `tied`); `unanimous` needs every voice to weigh in and agree (a failure vetoes it, `split`);
    `parity-pair` compares the proposer against every parity counterweight and escalates on disagreement.
    Outcomes: `unanimous` / `majority` / `no_majority` / `plurality` / `tied` / `split` / `agree` /
    `escalate` / `no_quorum`. `all-voices` still returns the every-voice shape.
  - Verdict extraction. Each voice's verdict is read the v2 way — the last `VERDICT: <token>` line
    (case-insensitive) by default, or the last JSON object carrying a non-empty `verdict` field when a
    `verdict_schema` is given (a balanced-brace scan, so a trailing footer object or a truncated array
    cannot steal the vote). A voice with no extractable verdict is `unparseable` — recorded with a reason,
    never silently dropped, and kept in the denominator.
  - Server-side synthesis. `synthesize` (tri-state, defaulting to `synthesize_default`, off out of the
    box) runs a combining pass for an `all-voices` panel on a fresh read-only ACP turn — the nominated
    `judge` if named, else the first successful voice — and records `synthesis` / `synthesis_by`.
  - Diversity scoring. A `DiversityReport` is computed from the answering voices' provenance —
    answered-voice count, distinct models, distinct providers, unknowns — and flags `low_diversity` when
    at least two voices resolve but collapse below `min_distinct` distinct models *or* providers.
  - `min_quorum` / `no_quorum`. An aggregating strategy with fewer than `min_quorum` usable (ok +
    parseable) voices returns `no_quorum` instead of a decision.
  - `expand_all` / auto-panel. Omit `targets`, pass an empty list, pass the sentinel `"all"`, or set
    `expand_all=true` to fan out to every registered agent (capped at `max_targets`), with each excluded
    agent recorded in `skipped` with its reason.
  - Per-seat `Target` metadata. A `{cli, model}` target may carry `role` (a per-seat role override),
    `weight` (for `weighted`), `parity` / `stance` (for `parity-pair` and steering); the strategies read
    the seat's metadata even though the ACP turn rebuilds the result's bare `(cli, model)` target.
  - The dropped `consensus` parameters are back: `strategy`, `verdict_schema`, `judge`, `stances`,
    `synthesize`, and `expand_all`, alongside the existing ones. `mode="async"` runs the same aggregating
    path off the request path.
- An `activity` tool: a focused snapshot of the background work in flight right now. Where `list_jobs`
  enumerates every tracked job of every status, `activity` returns only the running and pending jobs —
  `{active: [...], count}`, each row `{job_id, tool, status, summary, started_at, elapsed_s}` with a live
  `elapsed_s` measured against the store's clock and the rows sorted longest-running first. It returns
  `{active: [], count: 0}` when nothing is in flight.
- A `setup` first-run helper (the "good duck" getting-started surface). It resolves the config path for a
  scope — `project` (`<cwd>/.rutherford/config.toml`) or `global` (the platform config dir's
  `config.toml`) — and returns a sensible commented starter `config.toml` at the effective defaults
  (`default_safety_mode`, `default_timeout_s`, `auto_detect_local_models`, `max_targets`, a commented-out
  `[agents.local-goose]` local-model example, and a `trusted_workspaces` line), plus a snapshot of the
  agents already registered. With `write=true` it creates the file but never clobbers an existing one
  (`already_exists=true`, `written=false`); with `write=false` (default) it returns the proposed `content`
  and `path` without touching disk. `trust_workspace=true` adds the current directory to
  `trusted_workspaces`. The generated TOML round-trips through `tomllib` and validates against
  `RutherfordConfig`. An invalid scope fails on the request path with `INVALID_INPUT`.
- Role personas (the "good duck" role surface, over the ACP core). A `role="<id>"` argument on
  `delegate` / `consensus` / `debate` prepends a reusable system prompt to the caller's task (the role
  text, a `---` delimiter, then the prompt). Five substantive built-ins ship as package data:
  `principal-reviewer`, `architect`, `debugger`, `security-reviewer`, and `explainer`. A `role_dirs`
  directory adds new roles or overrides a built-in of the same id; each role is a markdown file with a
  small `name` / `description` frontmatter block whose body is the prompt. Loading is tolerant — a
  missing directory or a malformed role file is logged and skipped, never a startup crash. A new
  `list_roles` tool enumerates the catalog (`{roles: [{id, name, description}]}`), and a bad `role` id
  fails on the request path with the new `UNKNOWN_ROLE` code listing the known roles.
- Async background jobs. `delegate` / `consensus` / `debate` take `mode="async"` to run the work off the
  request path: the call returns a small `{job_id, status, tool}` envelope immediately and the work runs as
  an in-memory `asyncio` task. Four tools manage them — `list_jobs` (light listing, newest first),
  `job_status` (status + timings), `job_result` (the finished run's envelope, byte-for-byte the same as the
  sync path, or a structured error when failed/cancelled/not-done), and `cancel_job` (kill a running job).
  The store is bounded by `max_jobs` (evict the oldest finished, else `TOO_MANY_JOBS`) and `job_ttl_s`
  (finished jobs expire on access); a background task captures any exception so it can never crash the
  server. In-memory only — jobs clear on restart (durable, replayable runs are the separate F2 corpus).
- Nine more agents in the ACP-native roster (v3): `codex` and `claude_code` via the official Zed adapters
  `codex-acp` / `claude-agent-acp` (both reuse the existing CLI login over ACP — no API key — keeping the
  ChatGPT and Claude Code logins, correcting the earlier research note that flagged them as possibly
  API-key-only), plus `copilot` (`copilot --acp`), `qwen` (`qwen --acp`), `droid`
  (`droid exec --output-format acp`), `cursor` (`cursor-agent acp`), `kiro` (`kiro-cli acp`), `pi`
  (the `pi-acp` wrapper) and `hermes` (`hermes acp`), each probed and driven live. Roster: 16 descriptors.
  (`hermes` is registered but kept out of the bounded integration test — the Nous endpoint latency is too
  variable to assert against; check it live with `doctor`.)
- Config-driven agents. Under ACP an agent is just how to launch it plus a few quirks (no per-CLI parser),
  so the roster is now built from the curated built-in defaults plus a `[agents.<id>]` config section.
  A config entry overrides a built-in agent's command/env/provider/model/handshake, disables one with
  `enabled = false`, or defines a brand-new agent (any unknown id, which must supply a launch `command`);
  `enabled_agents` restricts the result. The launch fields mirror the Zed/Cline `acp.json` shape.
- Zed/Cline `acp.json` import. The loader auto-discovers an `acp.json` beside the global config and in the
  project's `.rutherford/`, folding its `agent_servers` into the agents config the way Zed/Cline read it.
  The native TOML wins over an imported `acp.json` at the same scope; an import never overrides a built-in
  or blocks startup when malformed.
- Local-model backends (Ollama / LM Studio) as first-class ACP voices. An `[agents.<id>]` entry with
  `base` (a built-in to clone), `backend` (`ollama` / `lmstudio`), `model` (and optional `host`) points an
  agent at a local runtime — Rutherford fills in the provider env. Supported pairs (all proven live):
  `goose` × ollama/lmstudio, `qwen` × ollama/lmstudio, `claude_code` × ollama (Ollama's
  Anthropic-compatible endpoint; LM Studio is OpenAI-only). The model must support tool-calling. See
  `docs/local-models.md`.

### Fixed

- **Write-sandbox hardening** (found by a second, deeper Codex-via-Rutherford safety review of the
  delegation / sandbox paths):
  - A sandboxed mode (`propose` / `write` / `yolo`) **with no `working_dir`** is now refused up front
    (`INVALID_INPUT`). Previously such a call fell through to the direct, un-sandboxed path and ran in the
    server's own cwd with writes allowed — and `trust_workspace=true` passed the trust gate regardless of
    `working_dir`, so this was a real unsandboxed-write bypass. A sandbox needs a tree to isolate; the call
    must name one.
  - The sandbox **apply-back is now containment-checked**: each changed/deleted path is resolved and refused
    unless it stays within the resolved `working_dir`. Without this, a symlink inside the workspace
    (`link -> /outside`) could redirect an applied edit to a file *outside* the trusted tree — a write escape.
  - `verify_read_only` now fingerprints the tree **whether or not the turn succeeded**. A read-only turn that
    mutated the tree and then failed (or returned empty) is now surfaced as `READONLY_VIOLATED` instead of an
    ordinary failure that hid the side effect.
  - The **non-git temp-copy sandbox now detects deletions**: a `write` / `yolo` agent that removes a file in
    the sandbox has the deletion applied back to the real tree, matching the git-worktree path (previously the
    non-git path lost deletions entirely).
  - The sandbox `open()` now runs **under a shield** so a cancellation mid-open cannot strand a half-built
    worktree (and its git admin entry) or temp copy — on a cancel the open is drained and cleaned up before
    the cancellation propagates.
  - A git `write` / `yolo` apply-back now **refuses to clobber an uncommitted local edit**. The worktree is
    built off `HEAD`, so a changed file carries `HEAD` + the agent's edit, not any uncommitted change the user
    has in the real tree; applying it back would silently overwrite that work. If any file the apply touches is
    dirty vs `HEAD` in the real tree (checked under the repo's own `autocrlf` policy, so a CRLF-checked-out file
    is not falsely flagged), the apply is refused with a clear "commit or stash first" error. An uncommitted
    edit to an unrelated file does not block. (A second-pass review finding.)
  - Further sandbox-robustness fixes (a third review pass of the apply-back):
    - The non-git temp-copy path now diffs the agent's edits against an **open-time content baseline** (per-file
      hashes), not the live tree — so a concurrent user edit to an *untouched* file is no longer mis-attributed
      as an agent change, and a concurrent edit to a file the agent *did* change is detected and the apply is
      refused (rather than silently overwriting it).
    - A git **rename is applied as delete-old + add-new** (`--no-renames` on the diff), so a repo with
      `diff.renames` enabled no longer leaves the renamed-from path behind.
    - The non-git copy now **skips symlinks** entirely instead of dereferencing them, so a symlink pointing
      outside the workspace can't pull external bytes into the sandbox; the real symlink is left untouched.
    - Delete-back resolves only a path's **parent** (not the final component), so deleting a workspace symlink
      removes the link itself rather than following it (or being wrongly skipped).
    - Apply-back **never writes through a destination symlink** (a fourth review pass): if the destination is a
      symlink it is replaced at its own in-tree location rather than followed, so a symlink can't redirect a
      write to another file the conflict checks never examined; a real directory at the destination is skipped.
  - Two limitations are deliberate and documented (in `acp/sandbox.py`), given the cooperative-agent threat
    model: the sandbox is cwd + path-guard isolation, **not an OS jail** (a write/yolo agent's own process or a
    terminal command can still write an absolute path outside it; OS containment is deferred); and the
    check-then-apply path has a **narrow inherent TOCTOU** (a user save in the sub-millisecond window between
    the clobber check and the copy is not caught — the same gap `git apply` / `git stash` have).
  - Known, accepted limitation (unchanged, and documented in `acp/sandbox.py` + the security docs): the
    sandbox confines a *cooperative* agent's ACP `fs`/terminal activity by cwd + the path-escape guard; it is
    NOT an OS jail. A write/yolo agent's own process (or a terminal command it runs) can still write an
    absolute path outside the sandbox. Full OS containment (Job Objects / ACLs) remains deferred. This is
    strictly safer than v2, which ran agents directly in the user's tree with no Rutherford-side sandbox.
- **Safety: `consensus` and `debate` now refuse a mutating / sandboxed `safety_mode`** (`propose` / `write` /
  `yolo`) and run read-only. A debate drives its voices over persistent ACP sessions, and a budgeted consensus
  drives its harvest sessions, **directly in the real `working_dir` with no per-turn worktree** — so a mutating
  mode on those paths would let an agent write straight into the user's tree, unsandboxed. Beyond the leak,
  there is no coherent way to merge edits from several agents into one working tree (the same reason delegation
  already refuses a mutating cross-target fallback). The guard lives in the *services* (the security boundary,
  not just the tool wrapper), so it holds no matter which caller set the mode; the error points the caller at
  `delegate`, which isolates a single agent in a worktree sandbox and applies the reviewed diff back. Write /
  propose work belongs to `delegate`; panels (`consensus` / `debate` / `review` / `plan`) are read-only
  deliberation. (Found by the Codex-via-Rutherford parity review.)
- A steered debate voice (stance `for` / `against`) now has its stance **re-embedded every round**, not just
  round 1 (v2 parity). The later-round delta prompt re-appends "Keep arguing in favor of / against the
  proposition."; without it a multi-round debate drifts toward the center as each voice accommodates the
  others, so the assigned side has to be restated each round to hold the adversarial framing. (The v3 delta
  still omits the voice's own prior position — the persistent session remembers it — which is the leaner
  ACP-native shape; only the stance reminder was missing.)
- Orphaned agent process trees. A wrapper adapter spawns the underlying CLI as a child; the ACP transport
  terminated only the direct child, leaving that CLI running, holding the working directory, and piling up
  across `doctor` probes. The session now reaps the agent's descendant tree on close.
- A relative `working_dir` is resolved to an absolute path before `session/new` (ACP requires absolute);
  a `working_dir` that points at a file now fails cleanly as a spawn failure instead of an internal error.
- The ACP stdin buffer is raised from asyncio's 64 KiB default to 16 MiB. A single `session/update` larger
  than the limit (e.g. kilo enumerating hundreds of OpenRouter models, or a large file read) previously
  raised "Separator is found, but chunk is longer than limit" and dropped the connection.
- `cline` now drives over ACP, but only with Cline's own service auth — a ChatGPT-subscription or
  OpenRouter provider configured in the desktop app does not reach the headless `--acp` path.
- A run's `duration_s` is now rounded to milliseconds (3 decimals) at every source — the `_reduce` and
  `_failed` paths in the ACP session, and the debate contribution that carries it — instead of serializing
  a raw `time.monotonic()` float like `0.0017640000442042947`. The long float tails made TOON output
  needlessly noisy and could make a substring assertion over the envelope (e.g. counting `"42"`) over-count
  on digits inside the duration, which flaked the consensus test run to run. Semantics are unchanged
  (`duration_s` stays a `float`); the output is just stable and readable.

### Changed

- The `setup` starter `config.toml` now scaffolds the F2 durability knobs (`default_persistence`, a commented
  `jobs_dir`) and `synthesize_default` at their effective defaults, and points at the sibling `panels.toon`
  for named multi-agent panels (with a note to call `reload_panels` after editing it) — closing the v2-setup
  parity gap where the scaffold only emitted the safety/timeout/roster basics.
- Config: `AdapterConfig` → `AgentConfig` (gains `command`/`env`/`provider`/`handshake_timeout_s`),
  `adapters` → `agents`, `enabled_adapters` → `enabled_agents`.

## [1.7.0] - 2026-06-13

### Added

- Topology observation and live transparency (N1). Rutherford now observes how wide a run actually
  fans out and surfaces in-flight work two ways, both fed from one structured activity-event stream.
  - Process topology: the runner samples each subprocess's local descendant count with psutil on a
    coarse timer and reports the peak. A consensus/debate result (and its persisted record) carries a
    `Topology` — `declared` (the intended width), `realized_delegations` (Rutherford's own delegations
    including fallback re-runs, summed across the voices or turns), and `observed_peak_agents` (the local
    descendant high-water mark, a floor — a CLI's remote agents are invisible). A single persisted
    delegation records its width-1 topology too, filling the slot the F2 record reserved from day one.
  - A new `activity` tool: a structured, per-voice snapshot of in-flight work across the running
    background jobs — job, tool, cli, model, role, status, elapsed, observed agents, budget left — read
    from the same activity stream. Distinct from `list_jobs` (which lists every job record); pass a row's
    `job_id` to `cancel_job`.
  - One activity stream, two sinks that never diverge: an async job buffers the structured events (and
    projects them into `job.progress` for the existing poll), while a synchronous `delegate` / `consensus`
    / `debate` call pushes them to the caller as MCP progress notifications (`report_progress`), gated on a
    client-supplied `progressToken` and silent otherwise.
  - An optional advisory aggregate-agent cap: `max_agents_advisory` flags a panel that fans out wider
    than the cap (`Topology.over_cap`) and logs a warning without blocking it; `enforce_agent_cap=true`
    refuses such a panel up front with the new `AGENT_CAP_EXCEEDED` code. Off by default — observe first.
  - Lineage env: a spawned child carries `RUTHERFORD_LINEAGE` (a count-first depth across nested
    Rutherford layers) and, for a panel voice, `RUTHERFORD_PARENT_RUN` (written for external/corpus
    correlation), alongside the existing `RUTHERFORD_DEPTH`.

## [1.6.0] - 2026-06-13

### Added

- Time budget + effort (F8a). Three previously-conflated concerns are now separate knobs:
  - `timeout_s` (unchanged) stays the per-call unresponsiveness fault: a stuck child has its process
    tree killed and the run fails retryably with `TIMEOUT`.
  - `time_budget_s` is a new wall-clock harvest deadline for a whole panel (`consensus` / `debate`),
    distinct from each voice's `timeout_s`. Consensus runs the voices under the deadline and, when it is
    reached, keeps the answered voices and cuts the in-flight ones, then aggregates/synthesizes over the
    harvested set as long as `min_quorum` usable voices remain; a debate runs each round under the
    remaining budget (an `asyncio.wait` deadline), cutting a round's in-flight turns and finalizing the
    transcript-so-far over the turns that completed. A harvest is a success: the result carries
    `stop_reason="budget"` and a `RunRollup` (issued / answered / cut / usable counts, quorum, elapsed,
    and the effort applied). A harvest that leaves fewer than `min_quorum` usable results is the one
    genuine failure, the new `BUDGET_EXHAUSTED` code (not retryable, not a health signal). `on_budget`
    picks the disposition: `harvest` (default, cut the stragglers), `continue` (the budget is advisory —
    run everything; an async-job consensus *detaches* at the deadline, publishing the best-effort
    answered-so-far set as an interim result a poller can read while the stragglers keep running, then the
    full set when they land), or `resume` (cut the stragglers, intending a later deliberate come-back that
    rides the forthcoming job-continuation work — today equivalent to `harvest`, with the cut voice's resume
    handle recorded in the parent `state.toon`).
  - `effort` is a new producer "how hard may it think" hint with universal tiers `low` | `medium` |
    `high` | `xhigh`, mapped per adapter to that CLI's native knob (`map_effort`), clamped to the nearest
    supported tier, and reported as `effort_applied` so a budget that did nothing is never silent. Codex
    wires `-c model_reasoning_effort=<tier>` (on both the fresh and resume paths); Cursor rewrites the
    model id with its `-<tier>` reasoning suffix; CLIs with no effort knob report a no-op. Defaults come
    from `default_effort` / `default_time_budget_s` / `default_on_budget` in config (global or per-adapter
    `effort`). `effort` is available on `delegate`, `consensus`, and `debate`; the time budget on the two
    panel tools. The leaf and panel run records capture the requested and applied effort and the rollup.
  - In-flight work is no longer discarded on a cut. The process runner accumulates stdout in bounded
    chunks (teed on a new `on_stdout` channel, separate from stderr `on_progress`, and flushed even when
    a cut delivers no trailing newline) and returns what the child wrote before a deadline on
    `ProcessResult.partial`. A consensus voice cut at the deadline has that partial harvested *through the
    adapter's own parser* (gated by `AdapterCapabilities.supports_partial_output`, derived from
    `output_mode`): a JSONL/text voice yields a usable candidate answer that counts toward quorum and feeds
    the aggregation/synthesis (and any resume session handle is recovered for a later continuation), while
    a single-envelope voice (whose answer arrives only at the end) keeps its partial as a trace. A debate
    turn cut mid-flight keeps its streamed stdout as a trace too (never promoted to a stance). The harvested
    partial — answer or trace — is written into the voice's `artifacts/voices/voice-N.md`, with a cut
    voice's resume handle recorded alongside, so a kept job loses nothing. A persisted panel also tees each
    voice's stdout into a live `artifacts/voices/voice-N.live.md` *as it arrives* (off-thread, jobs only),
    so the in-flight stream survives a crash, not just a graceful finalization. A single delegation has no
    panel to harvest across (the degenerate case that "collapses toward timeout"), so a single run that
    hits its `timeout_s` keeps its pre-deadline stdout on the result's `partial` rather than dropping it.

## [1.5.0] - 2026-06-13

### Added

- Durable run persistence (F2), opt-in. A run can be kept as a job on disk: `persist=true` (or
  `default_persistence = "job"` in config) writes it under `<jobs_dir>/<run_id>/` as `state.toon` --
  a structured, versioned `RunRecord` (requested and resolved model, pinned argv, provenance, role,
  files, timing, status, cost, and a reserved topology slot) -- plus a Markdown `artifacts/answer.md`
  (and a `diff.md` for a write run). `jobs_dir` defaults to the workspace's `.rutherford/jobs`, so a
  kept run lives with the project. Durability is opt-in (Model A): an ephemeral run leaves nothing on
  disk, and a kept run's directory is returned as `run_dir` on the result.
  - Consensus and debate persist as well. A kept panel writes one parent record (`kind` consensus or
    debate) that links each voice by `run_id`, and every voice is a leaf record carrying its
    `parent_run_id`, so a reader can open the parent and walk to each voice. The parent's status is
    derived from the voices (succeeded when any voice answered, failed when none did) and it rolls up
    the panel's duration, safety mode, working directory, files, role, the union of the voices' changed
    files (consensus and debate alike), and the summed cost, plus the resolved panel orchestration config
    (the seat roster with per-target models and stances, the consensus strategy, `synthesize`, a debate's
    `rounds`, and any judge) so the panel itself -- not just each voice -- replays from the parent record.
    A consensus adds one `artifacts/voices/voice-N.md` per voice (plus a `voices/skipped.md`
    naming any auto-panel adapters left out and why); a debate adds a `transcript.md` -- so the parent
    explains itself even when no child records remain.
  - A persisted write run records its own `changed_files` delta -- the files it dirtied, minus those
    already dirty before it ran -- with the jobs directory excluded so a run never reports Rutherford's
    own bookkeeping as a change to the user's code. The saved `diff.md` captures both tracked-file
    changes and the full contents of files the run *created* (plain `git diff` omits untracked files).
  - The first time a workspace is used, Rutherford notes once that runs are ephemeral by default and
    how to keep one; for an unpersisted complex run (a panel, a mutating delegation, or a delegation with
    a fallback chain) it suggests `persist=true`. The notice rides both the sync result and the
    `mode=async` submit envelope. The
    `setup` tool takes a `default_persistence` (`ephemeral` | `job`) and a `scope` (`global` |
    `project`); `scope=project` writes the choice to the workspace's `.rutherford/config.toml` (now a
    loaded config location), answering the first-run hint for that workspace. `external_tracking=true`
    on `delegate` / `consensus` / `debate` silences both hints when a workflow already tracks run state.
  - Persistence is best-effort: a filesystem failure never fails a run that already produced an answer,
    it runs off the event loop, and it never persists the child process environment (it can carry
    secrets). Note: `state.toon` is written for a human or LLM to re-read; machine round-trip via the
    current TOON codec is a known limitation tracked for the reader-side roadmap items.

### Removed

- The config-driven generic adapter (`GenericAdapterConfig` / the `generic_adapters` config key, and
  `GenericSafetyConfig`). Every CLI is now a hand-written code adapter that reuses the shared
  parsing/result/provenance toolkit. No real coding CLI fit config alone -- each needs custom output
  parsing, an auth probe, session resume, or cost handling -- so the config-only path was a fiction.
  A config that defined `generic_adapters` should move those CLIs to code adapters; the key is no
  longer accepted (config load rejects unknown keys).

### Fixed

- Ollama adapter: pass `--nowordwrap` so `ollama run`'s interactive word-wrap renderer -- which runs
  even when stdout is a pipe -- no longer duplicates words at the ~80-column wrap boundary. The
  renderer printed the start of a word, erased the fragment with cursor escapes (`ESC[ND ESC[K`), and
  reprinted the whole word on the next line; stripping the ANSI afterwards left the orphaned fragment
  behind, corrupting any output line longer than ~75 characters. Thanks to @arondee for the fix.
- Antigravity adapter: re-verified against agy 1.0.8 and re-pinned the transcript layout. **Known
  limitation:** agy 1.0.8's print mode applies file edits even without the bypass flag and offers no
  read-only/deny flag (`--sandbox` restricts only the terminal), so `read_only` / `propose` are now
  *best-effort* on this adapter rather than guaranteed -- an agent that chooses to edit in a non-mutating
  mode will modify the workspace. Enable the optional `verify_read_only` git guard (it fails such a run
  `READONLY_VIOLATED` after the fact) for git workspaces where agy runs non-mutating, or use agy in
  `write` / `yolo` behind a trusted workspace, or name it explicitly rather than in the default
  read-only fan-out. Pending agy restoring a read-only print mode.

## [1.4.0] - 2026-06-12

### Added

- Three new built-in CLI adapters, bringing the roster to thirteen, each built and verified against
  the live binary:
  - **Droid** (Factory, `droid`): `droid exec --output-format json` (Claude-Code-style JSON
    envelope), `--auto low` / `--skip-permissions-unsafe` write tiers, auth via `FACTORY_API_KEY` /
    `FACTORY_TOKEN` or a persisted `~/.factory` login.
  - **Mistral Vibe** (`vibe`): `vibe --output json --agent <mode> -p`, the only adapter that adds a
    first-party Mistral/Devstral provider. Model selection via the `VIBE_ACTIVE_MODEL` env override
    (Vibe has no `--model` flag); auth via `MISTRAL_API_KEY` or a persisted `~/.vibe/.env`. The
    adapter forces `PYTHONIOENCODING=utf-8` so Vibe does not crash on non-cp1252 output on Windows,
    and relies on the runner's `DEVNULL` stdin for the EOF `vibe -p` waits on.
  - **GitHub Copilot CLI** (`copilot`): `copilot -p --output-format json` (JSONL), session resume,
    auth via a fine-grained GitHub PAT (`COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`) or a
    persisted `copilot` login. `--no-auto-update` is pinned, and only the documented `auto` model
    sentinel is advertised because Copilot rotates its concrete model ids.
- Gated real-CLI integration tests covering the safety ladder (write applies an edit, `read_only`
  does not), session-resume round-trips, and multi-line prompt integrity, parametrized over the
  whole roster.

### Changed

- The supported-CLIs tables (README and `docs/adding-a-cli.md`) and the confirmed-version table now
  include Droid, Mistral Vibe, and GitHub Copilot.

## [1.3.1] - 2026-06-11

Documentation-only release; no code changes. Re-published so the refreshed README and the
official-MCP-registry ownership marker reach PyPI.

### Added

- `docs/recipes.md`: a task-oriented cookbook of paste-able prompts (the how-to guides moved
  out of the README).

### Changed

- Restructured the README around a proof-first masthead (PyPI/Python/MIT/CI badges, one-click
  Cursor and VS Code install links, the install one-liner), the real debate transcript, and a
  five-minute quickstart with `doctor` as the pre-flight gate and a keyless local-model lane.
  The adapter matrix, strategies, and saved panels moved under collapsible sections. Added a
  per-CLI confirmed-version table (versions verified for 1.3.0) and an `mcp-name` marker for the
  official MCP registry.

## [1.3.0] - 2026-06-11

### Breaking

- A config-defined generic adapter must now declare its read-only posture: config load fails unless
  `safety.read_only` is a non-empty argv fragment OR `natively_read_only = true` explicitly declares
  the CLI cannot write or execute by default. Previously an omitted fragment silently ran the CLI in
  its native (possibly write-capable) posture while the result claimed `safety_mode=read_only`. An
  existing config that omitted both now fails at startup with the fix named in the error.
- `default_safety_mode` is now honored: when a `delegate` / `consensus` / `debate` call omits
  `safety_mode`, the configured default applies (it was previously documented but inert -- every call
  silently ran `read_only`). An explicit `safety_mode` always wins over config, and mutating modes
  remain gated by the trusted-workspace check.
- The `review` and `plan` tools no longer accept `safety_mode`; both are clamped to `read_only`.
  Their docstrings said "read-only" while forwarding `write`/`yolo` into the delegation; the name is
  now enforced. A mutating run is `delegate`/`consensus` by design.
- Claude Code's prompt (with any role preamble folded in) now rides stdin instead of argv, and
  `--append-system-prompt` is no longer used (`capabilities().supports_system_prompt` is now
  `false`). This lifts the ~32K Windows command-line ceiling on long prompts and survives the
  npm `.cmd`-shim newline truncation.

### Changed

- On Windows, a delegation whose composed prompt would exceed the ~32K command-line limit on an
  argv-transport CLI is refused up front as a retryable `CONTEXT_OVERFLOW` (advising a stdin-transport
  CLI or a shorter prompt) instead of failing opaquely as `SPAWN_FAILED` -- which, being an unhealthy
  code, also wrongly benched the adapter.
- `synthesize` on `consensus` and `review` is now tri-state: omitted defers to the configured
  `synthesize_default` (off out of the box), and an explicit `synthesize=false` overrides a
  `synthesize_default=true` that previously could not be turned off per call. `debate` is unchanged.
- The `review` tool accepts `cli` / `cli:model` target strings over MCP, matching `debate` (its body
  already handled them; only the wrapper signature had rejected them).
- An auto-expanded (`expand_all`) consensus panel probes its adapters concurrently instead of one at a
  time, so assembly no longer pays each probe latency in series and one hung CLI shim cannot stall the
  others; membership, skip ordering, and the `max_targets` cap are unchanged.
- A saved panel's `strategy` is validated as the typed `Strategy` enum, so `panel_overrides` naming an
  unknown strategy now fail as `PANEL_INVALID` at resolution instead of sailing through to a
  context-free error at the call.
- LM Studio and Ollama advertise prompt-style file context, and Ollama folds in-scope files into the
  prompt instead of silently dropping them.
- Adapter probing and diagnosis moved out of the MCP tool layer into a service (`services/probing.py`)
  behind a single shared fan-out, keeping the tool layer thin; the capabilities/doctor output is
  unchanged except as noted under Removed.

### Fixed

- The process runner no longer deadlocks when a CLI fills its output pipes before consuming a large
  stdin prompt (output drains now start before stdin is written), and a single stderr line over
  64 KiB no longer crashes the delegation (chunked stderr drain). The timeout/cancel tree-kill now
  waits for killed survivors to be reaped and is shared with the metadata probe, so a timed-out
  wrapper's forked CLI is reaped on both paths.
- One raising adapter probe no longer aborts an entire consensus or debate panel: `detect()` and
  `fallback_model()` are contained as structured failures, and panel fan-out folds any escaped
  exception into that one voice.
- `[adapters.<id>] enabled = false` now disables a config-defined generic adapter too, and duplicate
  generic adapter ids fail registry construction instead of silently overwriting each other.
- Cursor's safety mapping fails closed: an unknown (future) safety mode now maps to `--mode ask`,
  not Cursor's edit-capable default.
- Antigravity's write-equals-bypass posture is surfaced instead of silent: a `write_uses_bypass`
  capability flag, a `doctor` note, and the safety-flags note all state that `write` and `yolo` are
  equivalent on this adapter (agy print mode has no granular approval).
- A truncated trailing JSON array in a model's answer can no longer steal the "last object" from the
  real verdict on the consensus strategy path, and OpenCode cumulative-snapshot streams (including
  interleaved multi-part streams) no longer return doubled text.
- A negative seat `weight` in a `panels.toon` now fails at load as `PANEL_INVALID` (naming the file
  and seat) instead of as a raw validation error mid-call.
- `setup` validates `safety_mode` at the tool boundary (a bogus value can no longer be written into
  `config.toml` where it would block the next startup) and applies its plan with exclusive-create
  writes, so a file appearing between planning and applying is skipped, never clobbered.
- An unexpected server error now returns a fixed message to the MCP client while the full traceback
  goes to the server-side log (exception text could previously leak paths or input to the client).
- Error codes are typed as the closed `ErrorCode` contract end to end: a typoed code now fails at
  construction instead of serializing into a client-visible envelope (the wire shape is unchanged).
- Antigravity's newest-conversation fallback tolerates a brain/ directory vanishing mid-scan, the
  probe cache keys results by effective timeout (a short-budget `timed_out` verdict is never served
  to a longer-budget call), and the short OpenAI model families (o1/o3/o4) are matched as whole
  tokens so an unrelated segment cannot mis-infer the provider.
- An adapter's `detect()` probe ran synchronously inside the async delegation path, so a probe-cache
  miss (cold start or past the TTL) blocked the event loop -- stalling every concurrent job, consensus
  voice, and progress stream -- until it returned; it now runs in a worker thread like every other
  probe call site.
- An exception other than a timeout or cancellation escaping the subprocess wait (an `OSError` from
  the stdin feed, a raising progress callback) left the child process alive and untracked; the runner
  now kills the whole process tree on any such escape before re-raising.
- Antigravity could return the previous turn's answer as the current run's result on a resumed
  conversation: the transcript scan was whole-file, so a new turn that produced no fresh answer
  surfaced the stale one, and prior history disabled the schema-drift canary. The scan is now scoped to
  events after the last user input, so a resume with no new answer fails as `TRANSCRIPT_NOT_FOUND` and
  the `CONTRACT_MISMATCH` canary fires again.
- Codex misreported two real event streams: a transient `error` event (a retried stream hiccup)
  latched a failure past a successful `turn.completed`, and on a non-zero exit any interim agent
  narration was treated as the answer even when the turn actually failed. The terminal `turn.completed`
  now clears a recovered error, and a non-zero exit carrying a failure is reported as a failure.
- OpenCode collapsed a legitimate delta stream to a single chunk whenever its longest chunk repeated
  anywhere in the stream; the snapshot-vs-delta resolution now uses a prefix rule, so a delta stream
  with a repeated line concatenates in full while a cumulative-snapshot stream (including a repeated
  final snapshot) still resolves to the latest snapshot. OpenCode's success/failure decision is also
  unified with the shared finalizer: a non-zero exit that still produced an answer is a success
  (matching Codex), and the full answer is no longer copied uncapped into the error message.
- Qwen's role preamble was passed as an argv element, which the Windows npm `.cmd` shim truncated at
  the first newline -- silently dropping the rest of the preamble and the `--add-dir`/`-r` arguments
  after it. The preamble now folds into the stdin prompt (`supports_system_prompt` is `false`), and the
  error `subtype` is read with the same string-coercing reader as the result field (a dict subtype
  could previously reach the user-facing message).
- A `RutherfordError` raised inside an async (`mode="async"`) job body was flattened to `INTERNAL`,
  dropping its specific code and details; the async path now preserves the code/message/details like
  the synchronous path, so an `INVALID_INPUT` stays `INVALID_INPUT`. Separately, `job_result` on a
  cancelled job returned the literal payload `null` and now returns a structured cancelled notice.
- A degenerate deeply-nested bracket run in a model's answer raised `RecursionError` (not
  `JSONDecodeError`) out of the JSON scanners, crashing consensus aggregation; the scanners now treat
  it as unparseable, honoring their never-raises contract. A malformed cost figure (a non-numeric
  token) likewise raised a validation error that sank an otherwise-good answer as `PARSE_ERROR`; cost
  extraction now degrades to no-cost while preserving the answer.
- The generic adapter's text mode reported empty stdout as a successful empty voice and skipped ANSI
  stripping; empty output is now a `PARSE_ERROR` and terminal noise is stripped, matching the shared
  text parser.
- A non-UTF-8 config or panels file (e.g. UTF-16 from PowerShell redirection) crashed with a raw
  `UnicodeDecodeError` instead of the structured `ConfigError` / `PANEL_INVALID`.
- Goose's snake-case serving-platform ids (`azure_openai`, `aws_bedrock`) are classed as serving
  backends, not model vendors, so they no longer inflate a panel's distinct-provider diversity count;
  and Codex's `doctor --json` auth parse falls back to the robust embedded-object scanner instead of a
  brace slice, so brace-bearing log noise no longer downgrades a Bedrock login to `NEEDS_LOGIN`.

### Removed

- The dead WSL path-translation layer (`runtime/paths.py` and `InvocationSpec.runtime`): it had no
  production caller and the process runner never consumed it, while the docs falsely claimed automatic
  translation. A config-defined generic adapter that sets a non-native `runtime` now fails at load with
  that named, since Rutherford launches CLIs natively; the `Runtime` enum and the advertised
  `capabilities.runtime` reporting are kept.
- The always-empty `artifacts` field (and the `Artifact` model) from the `DelegationResult` envelope:
  no adapter ever populated it, so a client reading it as "files changed" was always misled. The wire
  shape narrows accordingly. The duplicated top-level `runtime` field is likewise dropped from the
  capabilities/doctor adapter payload (the value remains under `capabilities.runtime`).
- The unreachable `UNSUPPORTED_SAFETY_MODE` error code and the unused `ALL_ERROR_CODES` /
  `is_error_code` helpers; `AUTH_REQUIRED` is kept and documented as reserved (pre-run auth gaps
  surface as panel skip reasons, mid-run rejections as `AUTH_FAILED`).
- Dead write-only `depth` fields on the request/context models and `InvocationContext.transcript_dir`
  (depth enforcement uses the `base_depth` parameter and `RUTHERFORD_DEPTH`), the single-consumer
  `strip_leading_reasoning` parsing helper, and two never-called panel-store methods. No behavior
  change.

### Added

- Gate hardening: `pytest -m integration` now FAILS when zero CLIs are opted in (set
  `RUTHERFORD_IT_ALLOW_EMPTY=1` to permit an empty run explicitly); the live model-selection and
  timeout tests assert real outcomes; optional local adapters without a configured `default_model`
  skip with the exact config named; a per-file coverage floor (80% across `adapters/`, `services/`,
  `runtime/`) and the entrypoint smoke check joined `just check` and CI; Python 3.13 joined the CI
  matrix.

## [1.2.0] - 2026-06-09

### Added

- Provenance on every result (F3): a `provenance` block records who actually answered -- the model
  vendor (`provider`), the serving platform when it differs from a direct vendor API (`backend`:
  bedrock / vertex / aws / openrouter / ...), the resolved model, the CLI version, and a `confirmed`
  flag marking a definitive signal from a heuristic guess. Each adapter derives its own provider
  (Claude Code's `CLAUDE_CODE_USE_*` backend, OpenCode's `provider/model` namespace, Goose's
  `GOOSE_PROVIDER`, the local runtimes, a shared vendor-from-model heuristic), and the delegation
  service stamps it, reusing the version it already detected. Every field is optional and dropped
  from the wire when unknown, so the result contract is unchanged for a caller that does not read it.
- Effective-diversity reporting on consensus and debate (F3): a `diversity` block shows how many
  distinct models and providers the answering voices actually spanned, with a `low_diversity` flag
  when the distinct-model *or* distinct-provider count collapses below the new `min_distinct` config
  floor (default 2). A panel that is one model in several CLI costumes -- increasingly likely as the
  roster goes bring-your-own-model -- is made visible instead of passing as N independent opinions.
- Cross-target fallback chains (F7): a `fallback` list of alternate targets on `delegate`. When the
  primary fails on a retryable category, each alternate is tried in order until one answers, and the
  result records the path in `fallback_chain`. Restricted to non-mutating modes (read_only / propose),
  since re-running a write task on a second CLI against the same tree would compound edits.
- A per-adapter cooldown (F7): after a few *unhealthy* failures (rate-limit, auth, timeout, spawn,
  output drift -- not a hard-task non-zero exit) within `cooldown_window_s`, an adapter is benched for
  `cooldown_duration_s`. A benched adapter is left out of an auto-expanded (`expand_all`) panel and
  skipped as a fallback candidate, but an explicit delegation to it still runs. Configured by
  `cooldown_threshold` (default 3; `0` disables), `cooldown_window_s`, and `cooldown_duration_s`.
- A typed failure taxonomy (F7): a generic non-zero exit is refined into a specific, stable error
  code by matching the error text -- the new `RATE_LIMITED`, `AUTH_FAILED`, `CONTEXT_OVERFLOW`,
  `MODEL_UNAVAILABLE`, and `SPAWN_FAILED` codes -- so a caller (and the fallback decision) can act on
  *why* a delegation failed rather than on an opaque `NONZERO_EXIT`.

### Changed

- Adapter output parsing is factored into a shared toolkit (F10): the JSON-object scanner, the JSONL
  splitter, the token-cost reader, and the stdout cleaners that were copied across every adapter now
  live in one place (`adapters/parsing.py`), behind two parser strategies (a JSON-envelope parser and
  a text parser) and a shared finalizer for the event-stream adapters. Behavior is preserved across
  all adapters; the config-driven generic adapter now uses the robust JSON scanner it had missed, so
  a prose-wrapped or pretty-printed JSON object is no longer dropped.

### Fixed

- A subprocess that failed to launch (a broken shim, a runtime error) previously propagated an
  uncaught `OSError` out of the delegation service; it is now a structured `SPAWN_FAILED` result like
  every other operational failure (F7).

## [1.1.0] - 2026-06-08

### Added

- An optional `judge` target on `consensus` and `debate`: the closing synthesis is performed by a
  named seat you choose rather than always by the first voice, so the panel can be judged by a
  neutral third party (or a stronger model) instead of by a participant. The result records
  `synthesis_by` (the label of the target that actually produced the synthesis), and with no `judge`
  the previous behavior -- the first ok voice synthesizes -- is unchanged. When no synthesis is
  produced (a named judge that cannot run, or empty output) `synthesis` and `synthesis_by` are both
  `None`, so the field never names an author for a synthesis that does not exist.
- A `plurality` consensus strategy: the top-scoring verdict wins even without a true majority (ties
  return `tied`). This is the lenient counterpart to the now-strict `majority`, so a caller can pick
  "most votes wins" explicitly instead of getting it by accident.
- A `max_concurrency` config field: a global semaphore in the delegation primitive bounds how many
  CLI subprocesses run at once across every panel (a wide consensus, a multi-round debate, nested
  self-delegation), decoupling panel width from host process pressure. When not set explicitly it
  defaults to `max_targets`, so raising the panel cap is not silently throttled to the old default;
  set it explicitly (or via `RUTHERFORD_MAX_CONCURRENCY`) to pin a different cap -- raise on a big
  box, lower on a laptop.
- A `min_quorum` config field (default 1): the minimum number of parseable voices an aggregating
  strategy needs before it returns a decision; below it the outcome is `no_quorum`. Guards against
  certifying an outcome off a single surviving voice when the rest failed.
- Opt-in `read_only` enforcement via a `verify_read_only` config field (off by default): after a
  *successful* non-mutating delegation whose working directory is a git repo, Rutherford fingerprints
  the tree under `working_dir` before and after the run -- status (with `--ignored=matching`) plus the
  unstaged and staged diffs, scoped to that subtree -- and fails the result with the new
  `READONLY_VIOLATED` code if it changed, turning the safety promise into a checked invariant. The
  content fingerprint catches a *further* edit to an already-dirty file (a status code alone would
  not) and a write to a gitignored path (`.env`, a cache dir); the subtree scope avoids attributing
  an unrelated change elsewhere in the repo. It is checked only when the run itself succeeded, so a
  real failure (timeout, non-zero exit, drift) is never masked. Off by default because it adds git
  calls per delegation; remaining limits: a write *outside* the repo is unobservable, and under
  concurrent fan-out on a *shared* tree a peer's write can still be attributed here (soundest for a
  single delegation -- worktree isolation gives per-voice soundness).
- An output-contract drift canary: an adapter can assert the machine-readable shape a successful run
  must have (`check_output_contract`), and a result that claims success but does not match is failed
  with the new `CONTRACT_MISMATCH` code instead of being trusted. The `claude_code` adapter asserts
  a JSON result envelope and `codex` asserts a JSONL event stream, and the `opencode`, `qwen`, and
  `cursor` adapters now assert theirs too, so a silent change to a CLI's `--json` output surfaces as
  a loud failure rather than a degraded answer.
- `list_jobs` and `cancel_job` MCP tools, plus a job lifecycle: background jobs now have a configurable
  `job_ttl_s` and a `max_jobs` cap (creating one past the cap fails with the new `TOO_MANY_JOBS`
  code), `list_jobs` enumerates them newest-first, and `cancel_job` cancels a running/pending job --
  killing its CLI process tree -- and records the new `cancelled` status. A lost job id is no longer
  unrecoverable, and a runaway async fan-out is bounded.
- Probe caching and a per-probe timeout ceiling: adapter metadata probes (`detect` / `check_auth` /
  `available_models`) are cached for `probe_cache_ttl_s` (default 10s) and capped at `probe_timeout_s`
  (default 8s), so `capabilities` / `doctor` / consensus auto-expansion no longer re-fork the same
  `--version` / status subprocesses each call, and a CLI whose probe hangs cannot stall the snapshot.
  `doctor` invalidates the cache before its live re-check.
- Structured logging to stderr (config `log_level` / `log_format`, JSON by default, `off` to silence):
  one JSON line per delegation and per job-lifecycle event, keyed on the correlation id that already
  flows through the services, so a failed panel is traceable. No prompt/response content is logged,
  and stdout (the MCP channel) is never written to.
- Fail-fast input and config validation: numeric config fields are bounded (a zero/negative
  `max_depth`, `default_timeout_s`, `max_targets`, etc. is rejected at load), a JSON generic adapter
  must declare `json_text_path` (and `jsonl`/`transcript` generic adapters are rejected -- they need a
  code adapter), `trusted_workspaces` / `role_dirs` are resolved to absolute paths with a warning on a
  missing directory (a typo'd trust path no longer silently never matches), and an unknown `cli` id in
  `consensus` / `debate` / `review` is one clean `UNKNOWN_TARGET` at the tool boundary instead of a
  buried failed voice.

### Changed

- **Consensus strategy semantics (behavior change for existing callers).** `majority` and `weighted`
  now require a *true* majority -- more than half of all eligible voices / summed weight -- and return
  `no_majority` otherwise; previously they returned the top-scoring verdict even below 50%. A 1.0.0
  caller using `strategy=majority` may now receive `no_majority` where it previously got a decision.
  The old "top scorer wins" behavior is available unchanged as the new `plurality` strategy, and
  `unanimous` / `parity-pair` now also count failed/unparseable voices (see Fixed). This is a
  deliberate correctness fix -- the old `majority` was effectively a plurality -- and ships in a minor
  because the project is pre-stable (Alpha), but callers that switch on `outcome` should review it.
- **Stricter config validation can reject a previously-accepted config at load.** Numeric fields now
  enforce bounds (a zero/negative `max_depth`, `default_timeout_s`, `max_targets`, `max_debate_rounds`,
  etc. is refused, with generous upper caps), a config-driven generic adapter with
  `output_mode = "jsonl"` or `"transcript"` is refused (those need a code adapter; 1.0.0 accepted them
  but silently returned the raw stream), and `output_mode = "json"` now requires `json_text_path`. A
  config relying on any of these now fails fast at startup with a clear `ConfigError` -- the intended
  firm-up, but an upgrade note. (`trusted_workspaces` / `role_dirs` are now resolved to absolute paths
  and a missing directory warns rather than failing -- it still fails safe.)

### Fixed

- Consensus strategy aggregation no longer certifies an outcome off the surviving voices alone. A
  failed or unparseable voice now counts in the denominator: `majority` and `weighted` require a true
  majority of *all eligible* voices (more than half), returning `no_majority` otherwise; `unanimous`
  treats any unparseable voice as a veto (the outcome is `split`, not a false unanimous); and
  `parity-pair` escalates when a designated counterweight fails to weigh in rather than agreeing off
  the survivors. Every eligible voice appears in the tally with either a verdict or a recorded
  `no_verdict_reason` (`failed` / `unparseable`), never a silent drop. A negative voting `weight` is
  rejected (it could shrink the denominator and fake a majority). (The old lenient "top scorer wins"
  behavior is still available as the new `plurality` strategy.)
- Verdict extraction is robust to messy model output. The balanced-object scanner parses from each
  JSON value start (so a stray quote or unmatched brace in the surrounding prose no longer hides the
  object), reads a nested object-valued field whole, does not descend into arrays (a trailing
  `[...]` list of objects can no longer steal the verdict from the real object), and picks the last
  object that actually carries a `verdict` (a trailing token-usage/"done" footer no longer shadows
  it). This replaces the non-nesting `{`-to-`}` regex that silently dropped many real verdicts.
- Two debate seats of the same CLI no longer collide. Each seat carries a distinct `seat_id` and a
  disambiguated transcript label (`claude_code`, then `claude_code#2`, `claude_code#3`); a generated
  `#N` suffix also skips any label already taken by an explicit one, so the transcript labels stay
  unique even when a caller hand-labels a seat `claude_code#2`. Positions stay separate through every
  round instead of one overwriting the other.
- The `codex` adapter now runs `codex exec` non-interactively so it works headless on Windows. For the
  sandboxed safety modes (read_only/propose/write) it passes `-c approval_policy=never`: without it the
  default approval policy blocks every command Codex deems "untrusted" (`rejected: blocked by policy`),
  because a spawned subprocess has no one to approve, so Codex could not even read files and silently
  degraded to answering from the prompt alone. On native Windows it also passes
  `-c windows.sandbox=unelevated`: Codex's default `elevated` sandbox needs UAC/administrator setup a
  nested, non-interactive process cannot complete (`windows sandbox: spawn setup refresh`), and
  `unelevated` is the documented fallback (a restricted token, no admin setup). The read-only sandbox
  still prevents writes; `approval_policy=never` only removes a prompt nothing could answer. The `resume`
  path carries the same overrides as `-c` config values (`_resume_safety_args` now preserves every flag,
  not just the sandbox mode), and the `map_safety` docstring no longer claims `codex exec` has no
  approval-policy control. See docs/troubleshooting.md.
- Adapter output parsing no longer turns a drifted CLI response into a confident wrong answer (an
  adversarial audit of every adapter's `parse_output`):
  - `claude_code` and `cursor` no longer return the literal string `"None"` (or an empty answer marked
    `ok`) when the JSON envelope's `result` field is null or missing -- that is now a `PARSE_ERROR`.
  - `qwen` no longer drops the real answer (carried in the assistant event) when the `result` event's
    field has drifted, and fails loudly if neither carries text.
  - the config-driven generic adapter no longer returns a Python `repr` of a non-string value when
    `json_text_path` resolves to a dict/list/bool (drifted shape) -- that is now a `PARSE_ERROR`.
  - `antigravity` no longer masks transcript-schema drift by returning unreliable stdout as a
    successful answer, and no longer attributes a *different* (stale or another project's)
    conversation's transcript to a run when the working dir is not in the index.
  - `lmstudio` no longer leaks an entire chain-of-thought when a reasoning model is truncated
    mid-`<think>` (now a `PARSE_ERROR`), and no longer deletes a literal `<think>...</think>` that
    appears inside a legitimate answer (the strip is anchored to a leading reasoning block).

### Documentation

- Document that the `lmstudio` adapter works with **LM Studio's LM Link**: a model loaded on another
  machine on your network is reachable by its normal model key (e.g. `openai/gpt-oss-120b`), so
  `capabilities` lists it and a delegation/consensus routes to that machine -- a panel can span
  several machines. No code change; LM Studio's `lms chat` handles the routing (use the plain model
  key, not a device-qualified one).

## [1.0.0] - 2026-06-08

### Added

- New built-in `lmstudio` adapter: delegate to a local [LM Studio](https://lmstudio.ai) model through
  `lms chat <model> -p "<prompt>"`, staying CLI-only -- it drives the `lms` command, never LM Studio's
  HTTP server, and JIT-loads the model (no separate `lms load` or running server). Bring your own
  model via the `model` argument (the LM Studio model key, e.g. `google/gemma-4-12b`) or
  `[adapters.lmstudio] default_model`; it has no built-in default. The role preamble rides in the
  native `-s` system-prompt flag, and `parse_output` strips the stdout load-progress bar and any
  `<think>...</think>` reasoning block so the answer is clean. Like `ollama`, it is `optional` (kept
  out of an auto-`"all"` panel) and honors `[adapters.lmstudio] timeout_s` / `extra_args` (e.g.
  `--ttl`).
- New built-in `ollama` adapter: delegate to a local model through `ollama run <model>` (prompt on
  stdin), which keeps Rutherford's CLI-only contract -- it drives the Ollama command, never the HTTP
  API. Bring your own model via the `model` argument or `[adapters.ollama] default_model`; the
  adapter has no built-in default, so with neither set it returns a clear error. A reasoning model's
  chain-of-thought is kept out of the answer (`--hidethinking`, a no-op on non-reasoning models), so
  pin a reasonably current Ollama. Sampling params come from the model's Modelfile; flags Ollama
  *does* expose (`--keepalive`, `--format`) can be set via `extra_args` (below).
- `[adapters.<id>] default_model` is now honored: when a delegation names no model, the configured
  default for that adapter is filled in (the field was documented but previously unused). This makes
  `delegate(cli="ollama")` work without naming a model on every call.
- Two new per-adapter config fields under `[adapters.<id>]`: `timeout_s` overrides the global
  `default_timeout_s` for one adapter (useful for a slow local model whose cold load exceeds the
  global budget), and `extra_args` appends extra CLI flags to the invocation (honored by the
  `ollama` and `lmstudio` adapters, e.g. `["--keepalive", "30s"]` or `["--ttl", "3600"]`).
- An `optional` adapter flag, surfaced by `capabilities` and `doctor`. The `ollama` adapter is
  optional: an absent or model-less Ollama reads as "only if you want it", never as an error.
  Optional adapters are excluded from a `consensus` auto-`"all"` panel (and from the setup starter
  panel) unless named explicitly, so a slow local model never silently joins an otherwise-cloud
  panel; the `skipped` list records why.

## [0.2.0] - 2026-06-05

### Added

- Guided first-run setup, in two forms over shared logic: a `setup` MCP tool your agent drives
  conversationally, and a `rutherford-mcp-server init` CLI wizard. Both probe the installed CLIs,
  recommend a starter panel from the ones you are signed in to, and scaffold the main `config.toml`
  and a `panels.toon`. The MCP tool is a dry run by default (it returns the proposed files for
  review) and writes them with `apply=true`; the CLI prints the plan and writes on confirmation
  (`--yes` to skip the prompt). Neither overwrites an existing file unless `force` / `--force` is
  given.
- Consensus strategies: `consensus` takes a `strategy` (`all-voices` (default) | `unanimous` |
  `majority` | `weighted` | `parity-pair`), and a panel can set one. Any strategy other than
  `all-voices` asks each voice for a verdict and aggregates: `unanimous` agrees only if every voice
  matches; `majority` is a vote count; `weighted` sums target weights; `parity-pair` compares the
  proposer against the parity counterweights and escalates on disagreement. Verdicts are read from a
  final `VERDICT: <token>` line, or from a JSON object when a `verdict_schema` is given; a voice that
  yields no verdict is `unparseable` -- still returned, excluded from the tally. The result is a
  `StrategyResult` with an `outcome`, a `decision`, and every voice's verdict and full answer. With
  no strategy (or `all-voices`), callers still get the legacy every-voice consensus shape.
- Per-target metadata: a consensus/debate/review target may now carry `role`, `label`, `weight`,
  `parity`, and `stance` alongside `cli` and `model` (as a dict, or via a saved panel). A
  per-target `role` overrides the call-level role for just that seat, `stance` steers just that
  voice (taking precedence over a parallel `stances` list), and `label` is the key the seat appears
  under in a debate transcript. `weight` and `parity` are carried for the consensus strategies. The
  legacy target shapes (a `cli` or `cli:model` string, a `{cli}` or `{cli, model}` dict) are
  unchanged, and a plain target still serializes as just `{cli, model}`.
- Custom roles now layer across config scopes the same way panels do: after the built-ins and any
  configured `role_dirs`, Rutherford reads a `roles/` directory from `~/.rutherford/`, then
  `<cwd>/.rutherford/`, then `$RUTHERFORD_CONFIG_DIR/`, the closest scope winning a name collision.
  Each role records its `source` (`builtin` | `config` | `user` | `project` | `env`), now reported
  by `list_roles`. Role files may be markdown (the body is the system prompt) or TOON (a
  `system_prompt` field). A malformed role file is logged and skipped rather than crashing the
  server.
- Saved panels: a named, reusable set of targets defined in a `panels.toon` file, referenced by
  `panel="..."` on `consensus`, `debate`, and `review` instead of spelling out the targets each
  call (with optional one-off `panel_overrides`). Panels are discovered across
  `$RUTHERFORD_CONFIG_DIR`, `<cwd>/.rutherford/`, and `~/.rutherford/` and merged by name, the
  closest scope winning a name collision -- the same precedence the TOML config uses for a project
  `rutherford.toml` over the global `config.toml`. Files are TOON, read through the serialization
  seam (which gained a `decode` counterpart to `encode`). Loading is lazy and cached; a new
  `reload_panels` tool re-reads edits without a restart. Panel files are validated at load, with
  every problem (bad TOON, unknown CLI, malformed target) reported in one pass rather than failing
  on the first. New error codes `PANEL_NOT_FOUND` and `PANEL_INVALID`.
- A `debate` tool: several targets argue a question across multiple rounds and return the full
  transcript. Round one is each voice's independent answer; every later round shows a voice the
  other voices' latest positions and asks it to rebut and revise, so the panel actually argues
  instead of answering in isolation. The result's `rounds` hold every voice's answer at every
  round, so the discussion is fully retraceable -- the verbose record that a one-shot `consensus`
  drops. A voice that fails a round is recorded and falls out; the debate stops early once fewer
  than two voices remain. Optional `stances` keep a voice arguing for/against throughout, and an
  optional closing `synthesize` pass (on by default) states where the panel converged. The new
  `max_debate_rounds` config field (default 4) caps the rounds; the per-call `max_targets` cap
  bounds the panel.

## [0.1.2] - 2026-06-03

### Fixed

- `doctor` and `capabilities` no longer report a Bedrock-configured Claude Code or Codex as
  `needs_login`. When a CLI is pointed at a third-party cloud backend (AWS Bedrock, Google Vertex,
  Bedrock Mantle), its credential is an AWS/GCP chain rather than an `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` or a native login session, so the old API-key/session probe gave a false
  negative.
  - The `claude_code` adapter now reads the JSON body of `claude auth status`. A third-party
    `apiProvider` / `authMethod` (or a `CLAUDE_CODE_USE_*` switch) reports `unknown`, so `doctor`'s
    live round trip confirms real reachability instead of trusting a "configured" flag.
  - The `codex` adapter now consults `codex doctor --json` and trusts its `auth.credentials` check,
    which validates the effective credential -- including the built-in `amazon-bedrock` provider.
  Both adapters fall back to the previous env-key / persisted-session markers when those commands
  are unavailable on an older CLI.

## [0.1.1] - 2026-05-31

### Fixed

- The README logo image and the links to `docs/`, `CONTRIBUTING.md`, and `LICENSE` now use
  absolute GitHub URLs, so they render and resolve on the PyPI project page. Relative paths only
  work on the GitHub repository view.

### Changed

- The GitHub Release attaches only the built wheel and sdist; a `.gitignore` byproduct that
  `uv build` writes into `dist/` is no longer uploaded as an asset.

### Removed

- A stray `.antigravitycli/` working directory the Antigravity CLI left in the repo during local
  verification and that was committed by accident. It is now gitignored. Only a symlink path was
  ever tracked -- no credential or file contents.

## [0.1.0] - 2026-05-30

Initial release.

### Added

- Interface-driven orchestration core. The services depend only on the abstract
  `CLIAdapter` and `ProcessRunner` interfaces; every CLI-specific detail lives behind an
  adapter, so adding or removing a CLI is additive.
- Eight CLI adapters: `claude_code`, `codex`, `cursor`, `qwen`, `antigravity`, `kiro`,
  `opencode`, `goose`, plus a config-driven generic adapter so a well-behaved further CLI is a
  config entry.
- The MCP tool surface: `delegate`, `consensus`, `review`, `plan`, `capabilities`,
  `doctor`, `job_status`, `job_result`, and `list_roles`, exposed over stdio via FastMCP.
- A universal `SafetyMode` (`read_only` | `propose` | `write` | `yolo`), defaulting to
  `read_only`, mapped per adapter to that CLI's approval and sandbox flags. Write and yolo
  modes require explicit opt-in and a trusted-workspace check.
- Synchronous and background (job) execution, with parallel consensus across targets and
  optional per-target stance steering.
- Normalized `DelegationResult` envelope and a stable set of string error codes, serialized
  as TOON (Token-Oriented Object Notation) to cut MCP client token usage, behind a swappable
  serialization seam.
- Cross-platform process execution (Windows, macOS, Linux, and Linux under WSL): argv arrays
  with no shell strings, `PATHEXT`/`.cmd` resolution, process-tree termination on timeout,
  and Windows<->WSL path translation.
- A delegation depth guard propagated through `RUTHERFORD_DEPTH`, plus a per-request target
  cap, so a CLI-calls-itself chain is bounded.
- Versioned role personas (`planner`, `codereviewer`, `security`, `debugger`) loaded from
  markdown.
- Fake-based unit tests, adapter golden tests, a shared contract test over every registered
  adapter, the self-invocation depth-guard test, and a local-only integration suite (real
  CLIs, skipped in CI). The full house-convention scaffolding and docs set.
