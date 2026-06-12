# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Commands

```sh
uv sync                       # install deps (project + dev group) into .venv
uv run ruff check .           # lint
uv run ruff format .          # format (write)
uv run ruff format --check .  # format check (CI mode)
uv run mypy                   # type-check (strict)
uv run python scripts/check_license_headers.py   # license-header check
uv run pytest                 # unit tests only (integration deselected by default)
uv run pytest -m integration  # local-only real-CLI integration suite
```

A `justfile` wraps these: `just check` runs lint + format-check + license-check + typecheck +
unit tests (the pre-push gate); `just test-integration` runs the real-CLI suite. Run a single
test file with `uv run pytest tests/test_delegation.py`.

Recommended pre-push flow: `just check`, then `just test-integration` for whatever CLIs the
machine has installed and authenticated.

## Architecture

Rutherford is a stdio MCP server that orchestrates other agentic coding CLIs as headless
subprocesses. It delegates work to, and builds consensus across, those CLIs. It never calls
model provider APIs directly and never reimplements a CLI's features.

Layered, with dependencies pointing inward toward the domain:

```
MCP tool layer (FastMCP)      src/rutherford/server.py + tools/   thin wrappers, no business logic
        |
services (orchestration)      services/   delegation, consensus, jobs, roles
        |
adapters (CLIAdapter impls)   adapters/   one hand-written code adapter per CLI
        |
runtime                       runtime/    ProcessRunner, platform/WSL detection
        |
domain + config               domain/, config/, io/   models, enums, errors, error codes, config
```

### The two interfaces are the heart of the project

- `adapters/base.py` defines `CLIAdapter` (a `Protocol`). The core knows nothing about any
  specific CLI; it depends only on this interface. `build_invocation` is pure and returns an
  argv list (never a shell string); `parse_output` returns the normalized `DelegationResult`
  and is where all CLI-specific quirks live (for example, Antigravity's transcript-file read).
- `runtime/process.py` defines `ProcessRunner` (a `Protocol`). The real implementation uses
  asyncio subprocess with an argv list, enforces a timeout, and kills the whole process tree
  on timeout or cancellation. Services take both interfaces by constructor injection, so the
  entire core is testable with `FakeAdapter` and `FakeProcessRunner` and no real subprocess.

### Layering rules

- The FastMCP layer is thin: a tool validates input, calls a service, and returns the
  normalized envelope via `toolSuccess` / `toolError`. No orchestration logic lives there.
- Adapters are registered in `adapters/registry.py`, not imported by the core. The registry is
  a closed mapping that fails fast on an unknown id at startup.
- Adding a CLI is a code adapter: subclass `BaseCLIAdapter` and reuse the shared parsing toolkit
  in `adapters/parsing.py`. There is no config-only path. See `docs/adding-a-cli.md`.
- Argument arrays, never shell strings. Default `SafetyMode` is `read_only`; write and yolo
  modes are explicit opt-in behind a trusted-workspace check, and no adapter ever defaults to
  its bypass flag.

### Conventions

- Python 3.11+, fully type-annotated, mypy strict. Ruff for lint and format (120-char lines).
- Two-line SPDX license header on every source file (enforced by
  `scripts/check_license_headers.py`).
- Docstrings on the public API of the core layers (adapters, services, runtime, domain).
- Tool payloads are serialized as TOON behind the `io/serialize.py` seam.
- No emojis in source files unless a user-visible string clearly benefits.
