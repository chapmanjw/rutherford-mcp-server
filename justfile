# Rutherford task runner. Run `just` to list tasks.
# Mirrors the commands documented in AGENTS.md and CONTRIBUTING.md.

# Show available tasks.
default:
    @just --list

# Install all dependencies (project + dev group) into the uv-managed venv.
install:
    uv sync

# Lint with ruff.
lint:
    uv run ruff check .

# Apply ruff formatting and autofixes.
format:
    uv run ruff format .
    uv run ruff check --fix .

# Verify formatting without writing changes (CI mode).
format-check:
    uv run ruff format --check .

# Type-check with mypy (strict).
typecheck:
    uv run mypy

# Verify the short license header on every source file.
license-check:
    uv run python scripts/check_license_headers.py

# Run the unit suite only (integration tests are deselected by default).
test:
    uv run pytest

# Enforce the per-file coverage floor across all of src/rutherford (needs a prior test run).
coverage-per-file:
    uv run python scripts/check_per_file_coverage.py

# Run the local-only integration suite (real CLIs; FAILS if zero CLIs are opted in).
test-integration:
    uv run pytest -m integration

# The full pre-push gate. `scripts/gate.py` owns the stage list so there is ONE local definition of what
# the gate is -- it runs the stages, streams their output, and writes a machine-readable verdict to
# .tmp/gate-report.toon naming which stage failed and whether the tree was clean. `tests/test_gate.py`
# asserts those stages match CI's, which is how they stay in step. The individual recipes below still exist
# for iterating on one stage at a time.
check:
    uv run python scripts/gate.py

# Smoke-check the stdio server entrypoint (imports + builds the app; does NOT start the transport).
smoke:
    uv run python -m rutherford --smoke

# Boot the server for real and speak JSON-RPC to it -- the check `smoke` cannot do, because `--smoke`
# returns before `mcp.run`. Asserts initialize, tool registration, one real call, and that nothing but
# JSON-RPC reaches stdout.
server-boot:
    uv run python scripts/check_server_boot.py

# The same check against a FRESH, UNLOCKED resolve of the built wheel -- the dependency set a
# `uvx rutherford-mcp-server` user actually gets, which `uv sync --locked` never exercises.
server-boot-release:
    uv run python scripts/check_server_boot.py --wheel
