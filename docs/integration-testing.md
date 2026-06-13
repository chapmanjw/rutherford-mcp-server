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
| Droid (Factory) | `RUTHERFORD_IT_DROID` |
| Mistral Vibe | `RUTHERFORD_IT_VIBE` |
| GitHub Copilot CLI | `RUTHERFORD_IT_COPILOT` |
| Amp | `RUTHERFORD_IT_AMP` |
| Cline | `RUTHERFORD_IT_CLINE` |
| Continue | `RUTHERFORD_IT_CN` |
| Hermes Agent | `RUTHERFORD_IT_HERMES` |
| Junie | `RUTHERFORD_IT_JUNIE` |
| Kilo Code | `RUTHERFORD_IT_KILO` |
| Kimi Code | `RUTHERFORD_IT_KIMI` |
| OpenHands | `RUTHERFORD_IT_OPENHANDS` |
| pi | `RUTHERFORD_IT_PI` |
| Ollama (local, optional) | `RUTHERFORD_IT_OLLAMA` |
| LM Studio (local, optional) | `RUTHERFORD_IT_LMSTUDIO` |

A contributor with only Codex and Claude Code installed sets `RUTHERFORD_IT_CLAUDE=1` and
`RUTHERFORD_IT_CODEX=1`; the rest skip.

## Per-CLI setup

Commands are current best knowledge as of 2026-05-30 (2026-06-13 for the v2.0.0 additions: Amp, Cline,
Continue, Hermes, Junie, Kilo, Kimi, OpenHands, pi); verify against each CLI's own docs and re-check
after upgrades. The bundled `scripts/update-clis.ps1` / `scripts/update-clis.sh` update the installed
CLIs and report versions; see [cli-maintenance.md](cli-maintenance.md) for per-CLI status and known issues. Rutherford never logs in for you -- do the one-time interactive login (or
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

### Amp (`amp`)

- Install: per [ampcode.com](https://ampcode.com) (a native binary). `amp update` self-updates.
- Authenticate: `amp login`, or set `AMP_API_KEY`. Check with `amp usage` (it prints the signed-in account
  and credit balance — Amp is metered). read_only is best-effort (Amp's permission switches are
  settings-file values, not per-call flags).
- Smoke: `amp -x "say ok" --stream-json`

### Cline (`cline`)

- Install: `npm i -g cline` (see [github.com/cline/cline](https://github.com/cline/cline)). `cline update` self-updates.
- Authenticate: `cline auth` (configures the provider/model). There is no non-interactive auth check, so
  `capabilities` shows `unknown` and `doctor` verifies it live.
- Smoke: `cline --json --plan "say ok"`

### Continue (`cn`)

- Install: `npm i -g @continuedev/cli` (see [github.com/continuedev/continue](https://github.com/continuedev/continue)).
- Authenticate: `cn login`. No non-interactive check (`cn config` needs a TTY), so auth shows `unknown`
  and `doctor` verifies it live. The adapter reads plain text (`-p --silent`) rather than `--format
  json`, whose envelope is unreliable (it passes a model's own JSON through unwrapped).
- Smoke: `cn -p --readonly --silent "say ok"`

### Hermes Agent (`hermes`)

- Install: per [github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). `hermes update` self-updates.
- Authenticate: `hermes setup` / `hermes login` (a Nous Portal device-code, a provider key, or a copilot
  token); check with `hermes auth list`. One-shot mode (`-z`) auto-bypasses approvals, so read_only is
  best-effort.
- Smoke: `hermes -z "say ok"`

### Junie (`junie`)

- Install: per [jetbrains.com/junie](https://www.jetbrains.com/junie/) (a JetBrains CLI that auto-updates on launch).
- Authenticate: a JetBrains token (`junie.jetbrains.com/cli`) or a BYOK key (`--openai-api-key` etc.) under
  `~/.junie`. No non-interactive check, so auth shows `unknown` and `doctor` verifies it live.
- Junie **requires a real stdin handle**: launched with stdin detached it fails with "Incorrect function".
  Rutherford feeds the prompt on stdin, which satisfies this. Junie is slow (tens of seconds) — set a
  generous `[adapters.junie] timeout_s`. read_only is best-effort (no headless read-only flag).
- Smoke: `printf 'say ok' | junie --input-format text --output-format json --skip-update-check`

### Kilo Code (`kilo`)

- Install: `npm i -g @kilocode/cli` (see [github.com/Kilo-Org/kilocode](https://github.com/Kilo-Org/kilocode)). `kilo upgrade` self-updates.
- Authenticate: `kilo auth login` to configure a provider (a delegation needs provider creds, distinct
  from a Kilo Gateway login). Check with `kilo auth list`. Kilo spins a local server per run, so it is
  slow — set a generous `[adapters.kilo] timeout_s`. read_only is best-effort.
- Smoke: `kilo run --format json "say ok"`

### Kimi Code (`kimi`)

- Install: per [github.com/MoonshotAI/kimi-code](https://github.com/MoonshotAI/kimi-code) (Moonshot's `kimi-code`,
  **not** the legacy `kimi-cli`). `kimi upgrade` self-updates.
- Authenticate: `kimi login` (device code) or a provider via `kimi provider`; or set `KIMI_API_KEY` /
  `MOONSHOT_API_KEY`. Headless `-p` has one fixed permission posture (`--plan`/`--auto`/`-y` are
  interactive-only), so read_only is best-effort and write/yolo cannot escalate.
- Smoke: `kimi -p "say ok" --output-format stream-json`

### OpenHands (`openhands`)

- Install: per [github.com/All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) (a `uv` tool;
  `uv tool upgrade openhands`).
- Authenticate: `openhands login` (OpenHands Cloud) or a stored LLM key. No non-interactive check, so auth
  shows `unknown` and `doctor` verifies it live. The adapter always sets `PYTHONIOENCODING=utf-8` — without
  it OpenHands crashes printing glyphs to a Windows cp1252 pipe. `--headless` auto-approves, so read_only
  is best-effort.
- Smoke: `set OPENHANDS_SUPPRESS_BANNER=1 && openhands --headless --json -t "say ok"` (with UTF-8 stdio)

### pi (`pi`)

- Install: `npm i -g @earendil-works/pi-coding-agent`, or the installer at [pi.dev](https://pi.dev)
  ([github.com/badlogic/pi-mono](https://github.com/badlogic/pi-mono)). `pi update self` self-updates.
- Authenticate: set the provider key for your configured provider (default `google` → `GEMINI_API_KEY`; on
  a free account a HuggingFace or other provider key). `pi --list-models` confirms a provider is
  configured. read_only is genuine (the `--tools read,grep,find,ls` allowlist removes edit/write/bash).
- Smoke: `pi -p --mode json --tools read,grep,find,ls "say ok"`

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
