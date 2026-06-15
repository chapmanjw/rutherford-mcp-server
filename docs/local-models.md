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
| `base`    | the built-in agent to launch (`goose`, `qwen`, `opencode`, or `claude_code`) |
| `backend` | the runtime: `ollama` or `lmstudio` |
| `model`   | the model id the runtime serves (e.g. `qwen3:8b`, `openai/gpt-oss-20b`) — required |
| `host`    | optional `host:port`; defaults to `localhost:11434` (Ollama) / `localhost:1234` (LM Studio) |

You can define as many as you like (e.g. one per model) and mix them into a `consensus` or `debate` for a
fully local panel.

## Supported agent + backend pairs

Not every agent can target a local runtime, and the runtimes expose different API shapes. The pairs below
were vetted live (2026-06-14) by driving a real ACP turn against Ollama (`qwen3:8b`) and LM Studio
(`openai/gpt-oss-20b`). The ones marked ✓ each answered; the rest are documented honestly with the reason.

| base          | ollama | lmstudio | how it connects |
|---------------|:------:|:--------:|-----------------|
| `goose`       |   ✓    |    ✓     | Ollama natively; LM Studio via goose's OpenAI provider |
| `qwen`        |   ✓    |    ✓     | the OpenAI-compatible `/v1` endpoint both runtimes expose |
| `opencode`    |   ✓    |    ✓     | an `@ai-sdk/openai-compatible` provider declared inline via `OPENCODE_CONFIG_CONTENT`, pointed at `/v1` |
| `claude_code` |  ✓*    |    —     | Ollama's Anthropic-compatible `/v1/messages`; LM Studio is OpenAI-only. *Slow — see the note below |

`*` **claude_code over a local model is slow.** The claude-agent-acp adapter runs a full agentic loop over
the Anthropic wire, which a local model serves much more slowly than its native OpenAI path. With a capable
model (`qwen3:8b`) and a generous `timeout_s` it answers correctly; a tight timeout times out, and a weak
model (e.g. `llama3.2:3b`) can answer wrong. Prefer `goose` / `qwen` / `opencode` for a local model unless
you specifically want Claude Code's loop; if you use it, give it a long timeout and a strong model.

### Agents with no local backend, and why

These were checked and genuinely cannot be pointed at a local runtime through Rutherford's env-keyed config:

- **`codex`** — codex's custom model providers now require the OpenAI **Responses API** wire
  (`wire_api = "responses"`); `wire_api = "chat"` is rejected. Ollama and LM Studio speak only
  chat-completions, not the Responses API, so codex has no usable local provider. The `codex-acp` adapter is
  also auth-gated (it demands a ChatGPT/API-key login at `session/new`), and codex's standalone `--oss`
  flag is not reachable through the ACP adapter. Use one of the supported agents instead.
- **`hermes`** — hermes *can* talk to Ollama, but only via its own `~/.hermes` `config.yaml` (`model.provider`
  + `model.base_url`). Its `acp` mode reads that config and **ignores** the `HERMES_INFERENCE_PROVIDER` /
  `HERMES_BASE_URL` environment (the persisted config selection deliberately wins over the env). So a local
  hermes is a config-file edit, not a per-session env, and it does not fit Rutherford's env-keyed `backend`
  mechanism. To run hermes locally, set `model.provider: ollama` and `model.base_url` in `config.yaml`
  yourself, then use hermes as a normal (non-`backend`) agent.
- **`cursor`, `copilot`, `kiro`, `droid`, `junie`, `cline`, `vibe`, `pi`, `kimi`** — vendor-locked to their
  own services or no usable headless local endpoint over ACP, so no local backend.

Use `goose`, `qwen`, or `opencode` (or `claude_code` on Ollama, with the slowness caveat) to run a local
model.

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

# Local opencode on Ollama (opencode is configured entirely through an inline env)
[agents.opencode-ollama]
base = "opencode"
backend = "ollama"
model = "qwen3:8b"

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
- **`claude_code` local turn times out** — it is slow over a local model (see the support matrix). Raise
  `timeout_s` and use a capable model (`qwen3:8b`+), or switch to `goose`/`qwen`/`opencode`.
- **`opencode` local turn returns nothing / wrong model** — the local provider is declared inline via
  `OPENCODE_CONFIG_CONTENT`, which Rutherford fills in from `model`; don't also set a conflicting
  `OPENCODE_CONFIG` file env, and make sure `model` is the exact id the runtime serves.
- **`codex` / `hermes` rejected as a `backend`** — neither has an env-keyed local pair (codex needs the
  OpenAI Responses API wire that local runtimes don't speak; hermes' `acp` reads its `config.yaml` provider
  and ignores the env). See the support matrix for the details and the hermes config-file workaround.
