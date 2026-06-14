# Local models (Ollama and LM Studio)

Rutherford can drive a local model as a first-class ACP voice — useful for private, offline, or
zero-cost panels. The key idea: **Ollama and LM Studio are not agents, they are model backends.** They
serve an HTTP API (Ollama on `:11434`, LM Studio on `:1234`) but have no agent loop, no tools, no ACP. So
you don't add Ollama as an agent — you point an existing ACP agent at it as its model provider.

You declare a local agent in config with three lines: which agent to launch (`base`), which runtime
(`backend`), and which model (`model`). Rutherford fills in the right provider environment for that pair.

## Zero-config auto-detection

You usually don't need to declare anything. On by default (`auto_detect_local_models = true`), Rutherford
probes a running Ollama (`:11434`) and LM Studio (`:1234`) when it builds the registry and registers each
suitable model as a `goose`-based agent automatically:

- **Ollama** — only models that report the `tools` capability are registered (an agentic loop needs
  tool-calling; a model without it, like `gemma3:12b`, is skipped). The id is `ollama-<model>` with the
  colons slugged out, e.g. `qwen3:8b` becomes `ollama-qwen3-8b`.
- **LM Studio** — every non-embedding model id is registered (ids containing `embed` are skipped, since
  LM Studio does not expose tool capability). `openai/gpt-oss-120b` becomes `lmstudio-openai-gpt-oss-120b`.

A built-in agent or an explicit `[agents.<id>]` of the same id always wins — a detected model never
overwrites it. A backend that is down, slow, or unreachable is skipped (a short ~1.5s probe per endpoint),
so detection never blocks or breaks startup. Run `capabilities` / `doctor` to see what was found. Set
`auto_detect_local_models = false` to turn probing off and require explicit local-agent config.

The manual `[agents.<id>]` form below stays available — use it to pin a specific model, a remote host, or
a different base agent.

## Quick start

1. Start a backend with a **tool-capable** model loaded (see [Requirements](#requirements)).
2. Add an agent to your Rutherford config (`~/.config/rutherford/config.toml`, or the project's
   `.rutherford/config.toml`):

   ```toml
   [agents.local-goose]
   base = "goose"
   backend = "ollama"
   model = "qwen3:8b"
   ```

3. It is now a normal agent id — `delegate(cli="local-goose", ...)`, or use it in `consensus` / `debate`.
   Check it with `doctor`.

## Config reference

A local agent is an `[agents.<id>]` entry. The id (`local-goose` above) is the name you delegate to.

| field     | meaning |
|-----------|---------|
| `base`    | the built-in agent to launch (`goose`, `qwen`, or `claude_code`) |
| `backend` | the runtime: `ollama` or `lmstudio` |
| `model`   | the model id the runtime serves (e.g. `qwen3:8b`, `openai/gpt-oss-20b`) — required |
| `host`    | optional `host:port`; defaults to `localhost:11434` (Ollama) / `localhost:1234` (LM Studio) |

You can define as many as you like (e.g. one per model) and mix them into a `consensus` or `debate` for a
fully local panel.

## Supported agent + backend pairs

Not every agent can target a local runtime, and the runtimes expose different API shapes. These pairs are
proven:

| base          | ollama | lmstudio | how it connects |
|---------------|:------:|:--------:|-----------------|
| `goose`       |   ✓    |    ✓     | Ollama natively; LM Studio via goose's OpenAI provider |
| `qwen`        |   ✓    |    ✓     | the OpenAI-compatible `/v1` endpoint both runtimes expose |
| `claude_code` |   ✓    |    —     | Ollama's Anthropic-compatible `/v1/messages`; LM Studio is OpenAI-only, so this pair is unavailable |

The other built-in agents (`cursor`, `copilot`, `kiro`, `droid`, `junie`, `hermes`, `opencode`, `cline`,
`vibe`, `codex`, `pi`) are vendor-locked to their own services or do not expose a usable headless local
endpoint, so they have no local backend. Use one of the three above to run a local model.

## Requirements

**The model must support tool-calling.** Rutherford's agents drive an agentic loop (read files, run
commands), so the local model needs tool support. A model without it (for example `gemma3:12b`) handshakes
but fails the turn with `does not support tools`, or emits raw tool JSON instead of an answer. Pick a
tool-capable model — `qwen3`, `llama3.1`/`llama3.2`, `gpt-oss`, `qwen3-coder`, and similar all work. Bigger
models give cleaner agentic behavior; a 3B model answers simple prompts but is weak at the protocol.

### Ollama

```sh
ollama serve              # if not already running (serves :11434)
ollama pull qwen3:8b      # a tool-capable model
```

### LM Studio

LM Studio needs its local server running, not just a model linked:

```sh
lms server start          # serves the OpenAI-compatible API on :1234
lms ps                    # shows loaded models
```

Then reference a loaded model id (from `lms ps` / `GET /v1/models`), e.g. `openai/gpt-oss-20b`.

## Examples

```toml
# Local goose on Ollama
[agents.goose-ollama]
base = "goose"
backend = "ollama"
model = "qwen3:8b"

# Local goose on LM Studio (a big local model)
[agents.goose-lmstudio]
base = "goose"
backend = "lmstudio"
model = "openai/gpt-oss-20b"

# Claude Code driving a local model via Ollama's Anthropic-compatible endpoint
[agents.claude-local]
base = "claude_code"
backend = "ollama"
model = "qwen3:8b"

# A remote Ollama box
[agents.goose-remote]
base = "goose"
backend = "ollama"
model = "qwen3:8b"
host = "192.168.1.50:11434"
```

## Troubleshooting

- **`does not support tools` / raw tool JSON in the answer** — the model lacks tool support; switch to a
  tool-capable one (see [Requirements](#requirements)).
- **`doctor` shows `not_installed` for the base** — the base agent's CLI (e.g. `goose`) isn't installed; a
  local backend reuses that agent's launch.
- **LM Studio agent fails to connect** — confirm `lms server start` is running and `GET
  http://localhost:1234/v1/models` responds; the model linked via `lms link` is not served until the
  server is started.
- **`claude_code` + `lmstudio` is rejected** — by design: Claude Code needs an Anthropic-compatible
  endpoint, which LM Studio does not provide. Use `ollama` for `claude_code`, or use `goose`/`qwen` for LM
  Studio.
