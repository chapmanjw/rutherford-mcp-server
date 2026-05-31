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
  API-key variable and no `whoami`, but `agy` persists the OAuth token on disk at
  `~/.gemini/oauth_creds.json` (shared by the Gemini CLI family), so `doctor` detects auth from
  that file. `doctor` with `live=true` additionally confirms it with a real round trip.
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
