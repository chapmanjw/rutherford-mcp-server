# Rutherford task runner. Run `just` to list tasks.
# Mirrors the commands documented in CLAUDE.md and CONTRIBUTING.md.

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

# Enforce the per-file coverage floor on adapters/, services/, runtime/ (needs a prior test run).
coverage-per-file:
    uv run python scripts/check_per_file_coverage.py

# Run the local-only integration suite (real CLIs; FAILS if zero CLIs are opted in).
test-integration:
    uv run pytest -m integration

# The full pre-push gate: lint, format check, license header, type check, unit tests,
# the per-file coverage floor, and the entrypoint smoke check.
check: lint format-check license-check typecheck test coverage-per-file smoke

# Smoke-check the stdio server entrypoint (imports + starts FastMCP).
smoke:
    uv run python -m rutherford --smoke
