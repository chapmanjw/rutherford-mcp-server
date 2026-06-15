# Integration testing

The integration suite (`tests/integration/`) drives the real ACP agents end to end — `delegate`,
`consensus`, and `debate` (persistent sessions) against a live agent, not the fake one. It is
local-only: the agents and their logins are not present in CI, so these tests are marked `integration`
and deselected by default.

```sh
uv run pytest -m integration      # or: just test-integration
```

A plain `uv run pytest` (or `just test`) runs the unit suite only; the `integration` marker is
deselected by default. The unit suite never spawns a real agent — it uses the fake ACP agent in
`tests/fake_acp_agent.py`.

## How it runs

The suite drives each agent directly through `run_acp_turn` and the services. There is no per-agent
opt-in environment variable: a test simply launches the agent's ACP server and asserts the round trip.
An agent that is not installed, not signed in, or does not drive on this machine fails that test — so
run the markers for the agents you actually have, and expect the rest to fail (not skip).

The agents exercised, grouped by how they were confirmed:

- **Drive cleanly over ACP-stdio:** `goose`, `vibe`, `junie`, `opencode`, `cline` (cline only with
  Cline's own service auth).
- **Official Zed adapters, existing CLI login (no API key):** `codex` (`codex-acp`, reuses the ChatGPT
  login), `claude_code` (`claude-agent-acp`, reuses the Claude Code login). A longer budget, because
  the first turn also negotiates the underlying CLI's auth.
- **Probed live, existing CLI auth:** `copilot`, `qwen`, `droid`, `cursor`, `kiro`, `pi`.

Deliberately kept out of the bounded assertions: `hermes` (registered and functional, but the Nous
endpoint latency swings too widely to assert against — check it with `doctor` live) and `kilo` (its
free gateway works only in the interactive TUI, not a headless spawn).

## What the suite covers

Per working agent: a read-only delegation returns a normalized result and the agent answers a trivial
prompt. Across agents: a parallel `consensus` over two `goose` voices returns one voice per target, and
a multi-round `debate` over persistent sessions returns the per-round transcript. The full ACP stack is
exercised, not a mock.

## Preparing an agent

Rutherford never logs in for you. Sign in to each agent with its own flow (or set its API key) once, so
the headless ACP session can reuse it. The launch command per agent is in the
[README roster table](../README.md#the-agent-roster); confirm an agent drives with `doctor` before
adding its tests.

A few agents need an ACP shim installed alongside the CLI:

- `codex` → `npm i -g @agentclientprotocol/codex-acp` (provides `codex-acp`)
- `claude_code` → `npm i -g @agentclientprotocol/claude-agent-acp` (provides `claude-agent-acp`)
- `pi` → `npm i -g pi-acp` (provides `pi-acp`, which spawns `pi --mode rpc`)
- `vibe` → the `vibe-acp` launcher

For local-model agents (Ollama, LM Studio), see [local-models.md](local-models.md) — they reuse the
`goose` launch, so a working `goose` plus a running runtime with a tool-capable model is all they need.

## Recommended pre-push flow

```sh
just check            # lint, format check, license header, mypy strict, unit tests + coverage + smoke
just test-integration # the agents this machine has installed and authenticated
```

`just check` is the full gate CI runs (minus the integration suite). Run `just test-integration` for
whatever agents you have; expect a failure for any agent you have not installed or signed into.
