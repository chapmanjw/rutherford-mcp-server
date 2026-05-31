# Changelog

All notable changes to this project are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
