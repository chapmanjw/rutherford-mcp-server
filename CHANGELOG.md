# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

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
