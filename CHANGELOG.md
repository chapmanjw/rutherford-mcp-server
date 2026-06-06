# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
