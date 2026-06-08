# Integration testing

The integration suite (`tests/integration/`) exercises the real CLIs end to end. It is local-only:
the CLIs and their credentials are not present in CI, so these tests are marked `integration` and
deselected by default. Run them before pushing.

## Opt-in scheme

Enable only the CLIs you have. Each adapter runs in the suite only when its environment variable is
truthy (`1`, `true`, `yes`, `on`) AND the CLI is installed and authenticated. Anything not enabled,
not installed, or not authenticated is skipped with a clear reason rather than failing.

| CLI | Opt-in variable |
| --- | --- |
| Claude Code | `RUTHERFORD_IT_CLAUDE` |
| Codex | `RUTHERFORD_IT_CODEX` |
| Antigravity | `RUTHERFORD_IT_ANTIGRAVITY` |
| Kiro | `RUTHERFORD_IT_KIRO` |
| OpenCode | `RUTHERFORD_IT_OPENCODE` |
| Goose | `RUTHERFORD_IT_GOOSE` |
| Cursor | `RUTHERFORD_IT_CURSOR` |
| Qwen Code | `RUTHERFORD_IT_QWEN` |
| Ollama (local, optional) | `RUTHERFORD_IT_OLLAMA` |
| LM Studio (local, optional) | `RUTHERFORD_IT_LMSTUDIO` |

A contributor with only Codex and Claude Code installed sets `RUTHERFORD_IT_CLAUDE=1` and
`RUTHERFORD_IT_CODEX=1`; the rest skip.

## Per-CLI setup

Commands are current best knowledge as of 2026-05-30; verify against each CLI's own docs and
re-check after upgrades. Rutherford never logs in for you -- do the one-time interactive login (or
set the API key) yourself, so the headless runner can reuse the session.

### Claude Code

- Install: per [Anthropic's docs](https://docs.anthropic.com/en/docs/claude-code) (native installer,
  or `npm i -g @anthropic-ai/claude-code`). Native on Windows, macOS, Linux/WSL.
- Authenticate: `claude auth login` (subscription/OAuth), or set `ANTHROPIC_API_KEY`. A long-lived
  CI token is available via `claude setup-token`.
- Smoke: `claude -p "say ok"`

### Codex

- Install: `npm i -g @openai/codex`, or the native package. Native Windows is supported.
- Authenticate: `codex login` (ChatGPT plan), or set `OPENAI_API_KEY` (or `CODEX_API_KEY`). A
  persisted session lands at `~/.codex/auth.json`.
- Smoke: `codex exec "say ok"`

### Antigravity (`agy`)

- Install: the Antigravity CLI (Go binary, native on Windows). See the project's install docs.
- Authenticate: run `agy` once interactively to complete the Google account flow. There is no
  API-key variable and no `whoami`, and the token's location is not reliable across platforms, so
  `capabilities` reports auth as `unknown`. `doctor` (default `live=true`) confirms it with a real
  round trip and reports `authenticated`.
- Smoke: `agy -p "say ok"` -- note the print-mode model is fixed and the answer is read from the
  transcript file, not stdout.

### Kiro (`kiro-cli`)

- Install: per [Kiro's docs](https://kiro.dev/docs/cli/). Native on Windows, macOS, Linux/WSL.
- Authenticate: set `KIRO_API_KEY` (requires a Pro, Pro+, or Power subscription to mint a key), or
  persist a session with `kiro-cli login`. Check with `kiro-cli whoami`.
- Smoke: `kiro-cli chat --no-interactive "say ok"`

### OpenCode

- Install: per [opencode.ai](https://opencode.ai/docs/). Native on Windows, macOS, Linux/WSL.
- Authenticate: configure at least one provider -- a provider key (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`), OpenRouter, or a local Ollama model. Check with `opencode auth list`.
- Smoke: `opencode run --format json "say ok"`

### Goose

- Install: per [Block's docs](https://block.github.io/goose/). Native on macOS, Linux/WSL, Windows.
- Authenticate: set `GOOSE_PROVIDER` and `GOOSE_MODEL` plus the provider's key (for example
  `ANTHROPIC_API_KEY`), or run `goose configure`. In headless/CI environments without a keyring set
  `GOOSE_DISABLE_KEYRING=true`. Check configured state with `goose info -v`.
- Smoke: `goose run -t "say ok" --no-session`

### Cursor (`cursor-agent`)

- Install: per [Cursor's CLI docs](https://docs.cursor.com/en/cli/overview). Native on Windows,
  macOS, Linux/WSL (installs as a `cursor-agent` shim).
- Authenticate: run `cursor-agent login`, or set `CURSOR_API_KEY`. Check with `cursor-agent status`.
  On a free plan only the `auto` model is usable; named models need a paid plan.
- Smoke: `cursor-agent -p --output-format json --trust --model auto "say ok"`

### Qwen Code (`qwen`)

- Install: per [Qwen Code's docs](https://github.com/QwenLM/qwen-code) (`npm i -g @qwen-code/qwen-code`).
  Native on Windows, macOS, Linux/WSL.
- Authenticate: run `qwen` once for the Qwen OAuth flow, or use an OpenAI-compatible key
  (`OPENAI_API_KEY` with `--auth-type openai`, or `DASHSCOPE_API_KEY`). Qwen OAuth has no
  non-interactive check, so `capabilities` shows `unknown` and `doctor` verifies it live.
- Smoke: `qwen -o json "say ok"`

### Ollama (`ollama`) — optional, local

A local model rather than a cloud CLI, and entirely opt-in: skip this whole section if you do not
want local delegation. `capabilities`/`doctor` mark it `optional: true`, so an absent or
model-less Ollama reads as "only if you want it", never as a missing requirement.

- Install: the Ollama daemon (`brew install ollama` on macOS, or the installer from
  [ollama.com](https://ollama.com)); start it with `ollama serve` (or the desktop app).
- Authenticate: none -- a local daemon needs no credentials.
- Get a model: `ollama pull <model>` (or build a custom Modelfile). The adapter has no built-in
  default -- name a model per call with `model=`, or set `[adapters.ollama] default_model` in your
  config. The integration test delegates at the configured default, so set one before running it.
  Bring whatever model you like -- one adapter fronts them all.
- Residency/sampling are the daemon's: per-model `num_ctx`/`temperature` come from the Modelfile,
  and `OLLAMA_KEEP_ALIVE` governs how long a model stays loaded. Flags `ollama run` *does* expose
  (`--keepalive`, `--format`) can be set via `[adapters.ollama] extra_args`.
- Slow hardware: local inference on a CPU or iGPU (no discrete GPU) is slow, and the FIRST call to a
  model is slowest because Ollama reads the weights from disk into RAM (a cold load) -- and pulls the
  model first if it is not already present. That first round-trip can exceed the 300s default
  timeout. Pre-pull with `ollama pull <model>`, and raise `[adapters.ollama] timeout_s` (per-adapter)
  or the per-call `timeout_s`.
- Output quality is the model's, not the adapter's. The adapter passes through exactly what
  `ollama run` writes (after stripping the spinner's ANSI). Some GGUFs -- bleeding-edge or community
  quants -- ship a chat template that leaks control tokens (e.g. `<|channel>...<channel|>`) into the
  answer as literal text, and no `ollama run` flag strips them: `--hidethinking` only suppresses
  Ollama's *native* thinking channel, so it is inert on a model Ollama does not flag as a thinking
  model. Check `ollama show <model>` -- if `thinking` is absent from Capabilities, `--hidethinking`
  does nothing. The fix is upstream: a cleaner model/quant, a newer Ollama that supports the model's
  thinking, or a custom Modelfile that corrects the template. (Observed with the `gemma4`
  `hf.co/unsloth/gemma-4-12B-it-qat-GGUF` quants on Ollama 0.30.6.)
- Smoke: `printf 'say ok' | ollama run <your-model>`

### LM Studio (`lmstudio`) — optional, local

Another local model rather than a cloud CLI, and entirely opt-in: skip this section if you do not
want local delegation. `capabilities`/`doctor` mark it `optional: true`. The adapter drives
`lms chat <model> -p "<prompt>"` ("print response to stdout and quit"), which JIT-loads the model —
no separate `lms load` and no running `lms server` are required.

- Install: LM Studio (from [lmstudio.ai](https://lmstudio.ai)); the `lms` CLI ships with it (run
  `lms bootstrap` once if `lms` is not on PATH).
- Authenticate: none -- local inference needs no credentials (`lms login` is only for publishing to
  LM Studio Hub).
- Get a model: download one in the LM Studio app or with `lms get <model>`. The adapter has no
  built-in default -- name a model per call with `model=` (the LM Studio model key, e.g.
  `google/gemma-4-12b`; see `lms ls`), or set `[adapters.lmstudio] default_model`. The integration
  test delegates at the configured default, so set one before running it.
- Remote models via LM Link: a model loaded on another machine on your network (connected through LM
  Studio's LM Link; see `lms link status`) is reachable by its normal model key and runs on that
  machine -- no extra config, and `available_models` lists it. Use the plain model key, not a
  device-qualified `<deviceId>:<modelKey>` (which `lms chat` rejects). When a model exists on several
  devices, LM Studio prefers an already-loaded instance; to pin one, use `lms link set-preferred-device`.
- Output cleanup: `lms chat` streams the model-load progress bar to stdout and a reasoning model
  emits a `<think>...</think>` block; the adapter strips both so the answer is clean. Sampling lives
  in the model's LM Studio config; `--ttl` (residency) and other `lms chat` flags go in
  `[adapters.lmstudio] extra_args`.
- Slow hardware: same cold-load caveat as Ollama -- the first call loads the weights into RAM and on
  a CPU/iGPU can exceed the 300s default. Pre-load with `lms load <model>` (or a prior call with
  `--ttl`), and raise `[adapters.lmstudio] timeout_s`.
- Smoke: `lms chat <your-model> -p "say ok"`

## What the suite covers

Per enabled CLI: a read-only delegation returns a normalized result; model selection is honored
where supported; the timeout path returns a structured error. Across CLIs: a parallel consensus over
at least two enabled CLIs returns one voice per target. Self-invocation: a CLI delegating to its own
adapter returns a normal result, and the depth guard stops a self-referential chain at `max_depth`.

## Running the suite

```sh
# whichever CLIs you have, on Windows PowerShell:
$env:RUTHERFORD_IT_CLAUDE = "1"; $env:RUTHERFORD_IT_CODEX = "1"
just test-integration            # or: uv run pytest -m integration

# on macOS / Linux / WSL:
RUTHERFORD_IT_CLAUDE=1 RUTHERFORD_IT_CODEX=1 just test-integration
```

A plain `just test` (or `uv run pytest`) runs the unit suite only -- the `integration` marker is
deselected by default.

## Recommended pre-push flow

```sh
just check            # lint, format check, license header, mypy strict, unit tests + coverage
just test-integration # for whatever CLIs this machine has installed and authenticated
```

An optional sample pre-push hook that runs the unit suite can be added, but is not forced.
