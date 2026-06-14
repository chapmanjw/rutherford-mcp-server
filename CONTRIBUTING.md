# Contributing

Thanks for your interest in improving Rutherford.

## Development setup

Rutherford uses [uv](https://docs.astral.sh/uv/) for the dev workflow.

```sh
uv sync
```

## Checks

Every change must pass the same gates CI runs. The `justfile` wraps them:

```sh
just lint            # ruff check
just format-check    # ruff format --check
just license-check   # short SPDX header on every source file
just typecheck       # mypy, strict
just test            # pytest, unit suite only
just check           # all of the above + the per-file coverage floor + the entrypoint smoke check
```

`just format` applies formatting and autofixes. `just check` is the pre-push gate. Unit tests gate the
build: a failing test, or coverage below the floor, fails CI and blocks a release.

The unit suite never spawns a real agent. The ACP-native design makes the whole core testable with the
fake ACP agent in `tests/fake_acp_agent.py` — no real subprocess.

## Integration tests

The local-only integration suite drives the real ACP agents and is the gate to run before pushing. It
cannot run in CI (the agents and their logins are not present there).

```sh
just test-integration        # pytest -m integration
```

A test fails for any agent that is not installed, signed in, or able to drive on the machine, so run it
for the subset you have. See [docs/integration-testing.md](docs/integration-testing.md).

## Conventions

- Python 3.11+, fully type-annotated, mypy strict. No bare `Any` where a real type fits.
- Small, focused modules; one responsibility per file. Domain-driven names; no `utils` or `helpers`
  grab-bags.
- The orchestration core depends on the descriptor registry and the ACP runtime, never on a concrete
  agent. Agent-specific launch quirks live in an `AgentDescriptor`, not in the services.
- Argument arrays, never shell strings. Default to `read_only`; never default to a bypass posture.
- Adding an agent is config-driven — see [docs/adding-an-agent.md](docs/adding-an-agent.md). A new
  built-in `AgentDescriptor` must drive over ACP on a real machine (confirmed with `doctor`), join the
  roster table in the README, and join the integration parametrization unless its latency is too
  variable to assert against.
- A two-line SPDX license header on every source file.
- Tool payloads serialize as TOON behind the `io/serialize.py` seam.

## Pull requests

Keep pull requests focused on a single change. Describe what changed and how you verified it, and
update the CHANGELOG `## [Unreleased]` section.
