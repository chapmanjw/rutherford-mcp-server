# Adding an agent

Under ACP an agent is just how to launch it as an ACP server plus a few quirks — the protocol
negotiates output parsing, system prompts, file context, permissions, and resume, so there is no
per-agent code to write. Adding an agent is config, not a code adapter. There are three ways in,
roughly in order of effort.

## Finding agents: the ACP registry

The Agent Client Protocol maintains a **public registry of every agent and bridge that speaks ACP**:

- Browse: <https://agentclientprotocol.com/get-started/registry> (also listed at <https://zed.dev/acp>)
- Machine-readable: <https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json>

Rutherford can **discover registry agents you already have**: run `discover` (the MCP tool) or
`python -m rutherford discover` from a terminal. It fetches the registry, finds which listed agents are
installed on this machine (scanning PATH plus `~/.local/bin`, `~/.cargo/bin`, and `~/.<vendor>/bin` — it
even finds a custom-path install like Qoder's `~/.qoder/bin/qodercli/`), probes each with a real read-only
ACP round trip, and prints a ready-to-paste `[agents.<id>]` block for the ones that drive. It is
detect-only — it never downloads or runs `npx`. Add `--write` (CLI) or `write=true` (tool) to append the
proposal to your config (never overwriting an existing section). For an agent `discover` cannot place (a
download-only entry, or one you want to wire by hand), add it manually as below.

Rutherford is the ACP **client**, so anything that exposes an ACP **server** over stdio can be driven by
it — and every registry entry does. Two kinds of entry, both added the same way (the config below):

- **A CLI with native ACP** — its own ACP-server subcommand or flag (e.g. `gemini --acp`, `goose acp`,
  `qwen --acp`). Use that launch command directly.
- **An ACP bridge / wrapper** — a small published package that fronts a CLI which has *no* native ACP by
  spawning it and translating to ACP (e.g. `amp-acp` wraps Sourcegraph Amp, `codex-acp` wraps the Codex
  CLI). Its launch command is the bridge (which usually requires the underlying CLI installed + authed).

A bridge is just an agent whose `command` happens to be the wrapper. For example, Sourcegraph Amp (no
native ACP) via the registry-listed `amp-acp` bridge:

```toml
[agents.amp]
command  = ["npx", "-y", "amp-acp"]   # the registry bridge; spawns the real `amp` CLI underneath
provider = "anthropic"
# Bridge prerequisites live with the underlying CLI: `amp` installed + authed (amp login / AMP_API_KEY),
# and note amp-acp needs a PAID Amp balance (free credits do not work over ACP).
```

Caveats for a community bridge (vs a vendor-native ACP mode): it is **third-party** — pin a version
(`["npx", "-y", "amp-acp@0.8.1"]`), re-verify it after updates, and confirm it actually drives with
`doctor` before trusting it in a panel. The bridge inherits the underlying CLI's auth, billing, and
safety posture.

## 1. Define a new agent in config

If you have an ACP-capable agent that is not in the built-in roster, declare it with an
`[agents.<id>]` block. The only required field is the launch `command` — the argv that starts the
agent as an ACP server over stdio.

```toml
[agents.my-agent]
command  = ["my-agent", "--acp"]
provider = "openai"        # optional: the fixed model vendor, recorded as provenance
default_model = "gpt-5"    # optional: the model used when a call names none
handshake_timeout_s = 30   # optional: raise it for a heavyweight agent
env = { MY_AGENT_TOKEN = "..." }   # optional: env set for the subprocess
```

The id (`my-agent`) is the name you delegate to: `delegate(cli="my-agent", ...)`, or use it in
`consensus` / `debate`. Confirm it drives with `doctor` — the only trustworthy health signal is a
real ACP round trip.

This is the same shape Zed and Cline use in their `acp.json`, so if an agent documents an `acp.json`
launch entry, the fields map directly.

## 2. Import a Zed/Cline `acp.json`

If you already configure ACP agents for Zed or Cline, drop the `acp.json` next to the global config or
in the project's `.rutherford/` and Rutherford folds its `agent_servers` into the roster
automatically. Only the launch `command` and `env` are imported.

```json
{
  "agent_servers": {
    "my-agent": {
      "command": "my-agent",
      "args": ["--acp"],
      "env": { "MY_AGENT_TOKEN": "..." }
    }
  }
}
```

Rules of precedence: the native TOML config wins over an imported `acp.json` at the same scope; an
imported id that collides with a built-in is skipped (override a built-in explicitly in TOML instead);
a malformed file is logged and skipped, never a startup crash. See
[configuration.md](configuration.md#importing-a-zedcline-acpjson).

## 3. Override or clone a built-in

To change how a built-in agent launches, set `[agents.<id>]` with the same id and the fields you want
to change:

```toml
# Pin a model on a built-in.
[agents.claude_code]
default_model = "claude-sonnet-4-6"

# Replace a built-in's launch command (e.g. a pinned shim path).
[agents.goose]
command = ["/abs/path/to/goose", "acp"]

# Disable a built-in entirely.
[agents.cursor]
enabled = false
```

To run the same built-in under a second id — most often to point it at a local model runtime — clone
it with `base`:

```toml
[agents.local-goose]
base    = "goose"     # clone the built-in goose launch
backend = "ollama"    # point it at a local runtime
model   = "qwen3:8b"  # the model the runtime serves
```

`base` and `command` are mutually exclusive: clone a built-in OR supply a raw command, not both. A
`base` clone also inherits the built-in's reasoning-effort knob, so a `base = "codex"` / `"claude_code"`
/ `"cursor"` / `"cline"` / `"kiro"` / `"junie"` clone honors `effort` like the agent it clones; a clone
that supplies its own raw `command` has no knowable knob and is an honest effort no-op. See
[local-models.md](local-models.md) for the supported `(base, backend)` pairs and the requirements on
the local model.

## Adding a built-in agent (contributors)

A built-in agent earns curated launch quirks that a bare `acp.json` cannot express — the Windows
npm-shim resolution, a per-agent handshake budget, a fixed provider. To add one, append an
`AgentDescriptor` to `HIGH_FIDELITY` in `src/rutherford/acp/descriptors.py`:

```python
AgentDescriptor("my-agent", "My Agent", ("my-agent", "--acp"), provider="openai"),
```

The fields are documented in [architecture.md](architecture.md#the-agent-descriptor-and-registry).
That is the whole integration — there is no parser, no auth probe, no resume handler to write, because
the ACP runtime handles all of it.

The merge bar for a new built-in:

- It drives over ACP-stdio on a real machine (confirmed with `doctor`, not just a documented flag).
- It is added to the roster table in the README and the launch note in `descriptors.py`.
- If it answers reliably within a bounded timeout, it joins the parametrized integration test in
  `tests/integration/`. An agent whose endpoint latency is too variable (the Hermes case) is kept out
  of the bounded assertion and checked with `doctor` live instead.
- The unit suite stays green using the fake ACP agent in `tests/fake_acp_agent.py` — no new test
  requires a real subprocess.

If an agent only drives with config (a non-default launch, an env token), it belongs in config or an
`acp.json`, not in the built-in roster.
