# CLI maintenance playbook

A guide for a future maintainer (human or Claude) keeping Rutherford's CLI integrations working as the
third-party CLIs evolve. Rutherford's own core is stable; the risk surface is the **adapters**, which
drive independent CLIs whose headless flags, output formats, and auth mechanisms drift between releases.
This doc records each CLI's status, how it is tested, its known issues, and the loop for re-verifying
after an upgrade. Pair it with [adding-a-cli.md](adding-a-cli.md) (the contract for a new CLI) and
[integration-testing.md](integration-testing.md) (how to install/authenticate each CLI and run the live
suite).

## The maintenance loop

When a CLI ships a new version (they auto-update or you bump them):

1. **Update the installed CLIs.** Run `scripts/update-clis.ps1` (Windows) or `scripts/update-clis.sh`
   (macOS/Linux/WSL). It updates each installed supported CLI via its native updater (or the npm / uv
   command that owns it) and prints a before→after version table. Use `-CheckOnly` / `--check-only` to
   only report versions. CLIs with no safe non-interactive updater are reported version-only with a
   manual hint.
2. **Re-verify the flags.** For any CLI whose version changed, re-read its `--help` and compare against
   the invocation the adapter builds (the adapter's module docstring records the exact flags + the date
   they were verified). Watch for: a renamed/removed headless flag, a changed JSON/event field, a new
   permission model, a dropped `--version` format.
3. **Run the live suite.** `just test-integration` after exporting the opt-in vars for the CLIs you have
   (`RUTHERFORD_IT_<CLI>=1`; see the table below). The suite is parametrized over every adapter in
   `tests/integration/helpers.py::CLI_ENV`, so each CLI gets the full battery: read-only round trip,
   model selection, timeout path, multi-line-prompt survival, write-applies / read-only-does-not,
   resume round trip, and cross-CLI consensus.
4. **Fix + re-pin.** If a flag or output shape drifted, update the adapter (and its golden samples under
   `tests/parsers/<id>/`), bump the "verified" date in the module docstring and the supported-CLIs table
   in `adding-a-cli.md` + `README.md`, and note the change here. Run `just check`.
5. **Drift canary.** Most adapters set `check_output_contract` so a silent output-format change fails
   loudly as `CONTRACT_MISMATCH` at the delegation layer rather than returning a trusted-but-wrong
   answer. Antigravity additionally pins `verified_version` and fails an integration test when the
   running `agy` moves past the pin.

## Opt-in variables and the verified versions

The integration opt-in var per CLI, and the version each was last verified against (2026-06-13).

| CLI | id | Opt-in var | Verified | Update mechanism |
| --- | --- | --- | --- | --- |
| Claude Code | `claude_code` | `RUTHERFORD_IT_CLAUDE` | 2.1.177 | `claude update` |
| Codex | `codex` | `RUTHERFORD_IT_CODEX` | 0.139.0 | `npm i -g @openai/codex@latest` |
| Cursor | `cursor` | `RUTHERFORD_IT_CURSOR` | 2026.05.28 | `cursor-agent update` |
| Qwen Code | `qwen` | `RUTHERFORD_IT_QWEN` | 0.17.0 | `npm i -g @qwen-code/qwen-code@latest` |
| Kiro | `kiro` | `RUTHERFORD_IT_KIRO` | 2.7.0 | Kiro installer (manual) |
| OpenCode | `opencode` | `RUTHERFORD_IT_OPENCODE` | 1.17.5 | `opencode upgrade` |
| Goose | `goose` | `RUTHERFORD_IT_GOOSE` | 1.36.0 | Block installer (manual) |
| Droid (Factory) | `droid` | `RUTHERFORD_IT_DROID` | 0.144.2 | auto-updates (manual reinstall) |
| Mistral Vibe | `vibe` | `RUTHERFORD_IT_VIBE` | 2.14.1 | Vibe installer (manual) |
| GitHub Copilot | `copilot` | `RUTHERFORD_IT_COPILOT` | 1.0.62 | `npm i -g @github/copilot@latest` |
| Antigravity | `antigravity` | `RUTHERFORD_IT_ANTIGRAVITY` | 1.0.8 | auto-updates (re-verify transcript layout) |
| Amp | `amp` | `RUTHERFORD_IT_AMP` | 0.0.1781384294 | `amp update` |
| Cline | `cline` | `RUTHERFORD_IT_CLINE` | 3.0.24 | `cline update` |
| Continue | `cn` | `RUTHERFORD_IT_CN` | 1.5.45 | `npm i -g @continuedev/cli@latest` |
| Hermes Agent | `hermes` | `RUTHERFORD_IT_HERMES` | 0.16.0 | `hermes update` |
| Junie | `junie` | `RUTHERFORD_IT_JUNIE` | 26.6.8 (1892.26) | auto-updates on launch |
| Kilo Code | `kilo` | `RUTHERFORD_IT_KILO` | 7.3.45 | `kilo upgrade` |
| Kimi Code | `kimi` | `RUTHERFORD_IT_KIMI` | 0.14.2 | `kimi upgrade` |
| OpenHands | `openhands` | `RUTHERFORD_IT_OPENHANDS` | 1.16.0 (SDK 1.21.0) | `uv tool upgrade openhands` |
| pi | `pi` | `RUTHERFORD_IT_PI` | 0.79.3 | `pi update self` |
| Ollama (optional) | `ollama` | `RUTHERFORD_IT_OLLAMA` | 0.30.6 | winget / installer |
| LM Studio (optional) | `lmstudio` | `RUTHERFORD_IT_LMSTUDIO` | build efce996 | desktop app self-update |

## Cross-cutting issues to know

- **Best-effort `read_only` (no native sandbox).** Several autonomous agents have no headless read-only
  / plan mode, so `read_only` / `propose` are best-effort: an agent that chooses to edit can mutate the
  workspace. These adapters set `AdapterCapabilities.write_uses_bypass=True`, carry a SAFETY CAVEAT in
  their module docstring, and rely on the optional `verify_read_only` config guard (a post-hoc git check
  that fails such a run `READONLY_VIOLATED`). Verified live (each applied an edit in read_only mode):
  **antigravity, junie, hermes, openhands** (no flag at all), and **amp, kimi, kilo** (a headless posture
  that auto-runs tools with no per-call deny flag). Turn on `verify_read_only` for git workspaces where you
  run these non-mutating. The genuinely sandboxed new adapters, verified live to leave a file untouched in read_only,
  are **cline** (`--plan --auto-approve false` — plan mode alone is NOT enough), **cn** (`--readonly`), and
  **pi** (`--tools read,grep,find,ls`).
- **The Windows npm-shim launch quirk.** npm installs three shims per command (`cn`, `cn.cmd`, `cn.ps1`).
  `cmd.exe /c` truncates an argv element at its first newline (so a multi-line role preamble loses
  everything after line one) and does not forward a programmatic stdin pipe to some node shims. So
  `runtime/launch.py::prepare_argv` launches an npm shim through its sibling `.ps1`
  (`powershell -File`) on Windows, which preserves multi-line args and forwards stdin. The sibling is
  resolved by the resolved shim's full path (same directory), never by bare name, so a same-named `.ps1`
  elsewhere on PATH is never substituted. If a future npm CLI misbehaves on Windows, check this path
  first.
- **`auth: unknown` is normal.** Adapters whose auth has no cheap, non-interactive check report
  `unknown` from `capabilities`; `doctor` (default `live=true`) confirms with a real round trip. Do not
  "fix" these to `needs_login` — a false negative wrongly benches a working CLI.

## Per-CLI notes — the v2.0.0 additions

### Amp (`amp`)
- **Status:** working. `amp -x "<prompt>" --stream-json` (Claude-Code-compatible JSONL; the `result`
  event carries the answer). Native `.exe`, prompt as the `-x` value.
- **Tested:** golden `tests/parsers/amp/`, unit `tests/test_amp_adapter.py`, integration via
  `RUTHERFORD_IT_AMP`. Auth: `AMP_API_KEY` or `amp login`, checked with `amp usage`.
- **Known issues:** read_only is **best-effort** — Amp's only permission switches are settings-file values
  a pure `build_invocation` can't write, and `-x` execute mode auto-runs its tools (verified live that
  read_only applied an edit), so `verify_read_only` is the backstop. No `--model` flag (the mode picks the
  Claude model); `provider` is reported as `anthropic`. Amp is metered — watch the credit balance (`amp
  usage`).
- **Re-verify:** re-read `amp --help`; confirm the `--stream-json` `result` event still carries
  `result` / `is_error` / `session_id`.

### Cline (`cline`)
- **Status:** working. `cline --json [--plan] "<prompt>"` (JSONL ending in a `run_result` event).
- **Tested:** `tests/parsers/cline/`, `tests/test_cline_adapter.py`, `RUTHERFORD_IT_CLINE`. Auth:
  `cline auth` (configured provider); no non-interactive check, so `unknown` + `doctor`.
- **Known issues:** genuine read-only via `--plan --auto-approve false` — **plan mode alone is NOT enough**
  (verified live that `--plan` with the default `--auto-approve true` still applied an edit). The adapter is
  always explicit (never relies on the default act mode). `--thinking` supports every effort tier incl. xhigh.
  Resume is **not** supported (`supports_resume=False`): cline's `--id` resume mode rejects a headless
  follow-up prompt (verified live that both a positional prompt and piped stdin are rejected once `--id` is set).
- **Re-verify:** confirm the `run_result` event still carries `text` / `finishReason` / `usage`, and
  that `--plan` is still the read-only posture.

### Continue (`cn`)
- **Status:** working. `cn -p --readonly --silent "<prompt>"` (plain text).
- **Tested:** `tests/parsers/cn/`, `tests/test_cn_adapter.py`, `RUTHERFORD_IT_CN`. Auth: `cn login`; no
  non-interactive check (`cn config` needs a TTY), so `unknown` + `doctor`.
- **Known issues:** the adapter reads **plain text**, not `--format json` — Continue's JSON mode is not a
  stable envelope (it wraps a non-JSON answer as `{"response","status"}` but passes a model answer that
  is itself valid JSON straight through, observed `{"result": 42}` for a numeric reply), so `-p --silent`
  (which strips `<think>` blocks) is the robust capture. The prompt rides as a positional (cn does not
  read a programmatic stdin pipe on Windows); the PowerShell launch preserves the multi-line value.
- **Re-verify:** confirm `-p --silent` still prints just the answer; confirm `--readonly` / `--auto` are
  still the postures.

### Hermes Agent (`hermes`)
- **Status:** working. `hermes -z "<prompt>"` (one-shot; prints only the final answer text). Native
  `.exe`, prompt as the `-z` value, plain-text parse.
- **Tested:** `tests/parsers/hermes/`, `tests/test_hermes_adapter.py`, `RUTHERFORD_IT_HERMES`. Auth:
  pooled credentials, checked with `hermes auth list`.
- **Known issues:** one-shot auto-bypasses approvals → read_only best-effort (no read-only flag;
  restricting toolsets via `-t` is a future tightening once a read-only toolset name is pinned).
  `--yolo` is the only escalation. The one-shot stream carries no machine-readable session id, so resume
  is not surfaced (`supports_resume=False`).
- **Re-verify:** confirm `-z` still prints only the final answer (no banner / session line).

### Junie (`junie`)
- **Status:** working, but **slow** (tens of seconds; it drives several models internally) and with a
  hard launch requirement. `junie --input-format text --output-format json --skip-update-check` with the
  prompt on **stdin**.
- **Tested:** `tests/parsers/junie/`, `tests/test_junie_adapter.py`, `RUTHERFORD_IT_JUNIE`. Auth:
  JetBrains token / BYOK key under `~/.junie`; no non-interactive check, so `unknown` + `doctor`.
- **Known issues:** **requires a real stdin handle** — launched with stdin detached (`DEVNULL`) it
  aborts immediately with `Junie failed with the message: Incorrect function` (a Windows console I/O
  error). The adapter always sets `spec.stdin` (the prompt doubles as the pipe); the runner connects a
  pipe exactly when `spec.stdin` is set, so this is satisfied. No headless read-only flag (`--brave` is
  interactive-only) → best-effort. Cost is the **sum** across the `llmUsage` per-model entries. Set a
  generous `[adapters.junie] timeout_s`. Junie auto-updates on launch (the `.bat` shim applies a pending
  update); `--skip-update-check` prevents a mid-run update.
- **Re-verify:** confirm the `--output-format json` object still carries `result` / `sessionId` /
  `llmUsage`; confirm the stdin requirement (DEVNULL still fails).

### Kilo Code (`kilo`)
- **Status:** working, **heavy** (spins a local server/DB per run, ~30s+). `kilo run --format json
  "<prompt>"` (JSONL; text chunks + a `step_finish` event).
- **Tested:** `tests/parsers/kilo/`, `tests/test_kilo_adapter.py`, `RUTHERFORD_IT_KILO`. Auth: `kilo auth
  list` (a configured provider — distinct from a Kilo Gateway login, which `kilo profile` checks and
  which a delegation does not need).
- **Known issues:** read_only is **best-effort** — `kilo run` auto-runs its tools non-interactively even
  with no flag (verified live that read_only applied an edit), so `verify_read_only` is the backstop.
  `write` uses `--auto`, `yolo` uses `--dangerously-skip-permissions`. The `--variant`
  reasoning knob is provider-specific (not a uniform tier scale), so effort is a documented no-op — pass
  `--variant` via `[adapters.kilo] extra_args` when a provider supports it. Set a generous
  `[adapters.kilo] timeout_s`.
- **Re-verify:** confirm `kilo run --format json` still emits `text` parts + a `step-finish` part with
  `tokens` / `cost`, and that an error event still nests its message at `error.data.message`.
- **Account note:** `kilo models` lists paid-gateway models (`kilo/<provider>/<model>`) that require a
  Kilo paid sign-in; a default-provider account gets `PAID_MODEL_AUTH_REQUIRED` when selecting one. The
  default model (no `-m`) works without the gateway. The model-selection integration test skips on this
  access error (it guards flag drift, not account entitlement).

### Kimi Code (`kimi`)
- **Status:** working. `kimi -p "<prompt>" --output-format stream-json` (JSONL; the answer is the last
  `{"role":"assistant","content":...}` line). Native `.exe`; this is Moonshot's `kimi-code`, **not** the
  legacy Kimi CLI.
- **Tested:** `tests/parsers/kimi/`, `tests/test_kimi_adapter.py`, `RUTHERFORD_IT_KIMI`. Auth: `kimi
  login` (device code) / `kimi provider list`, or `KIMI_API_KEY` / `MOONSHOT_API_KEY`.
- **Known issues:** the permission flags `--plan` / `--auto` / `-y` are interactive-only and **do not
  combine with `-p`** (`kimi -p --plan` is rejected: "Cannot combine --prompt with --plan"). So headless
  prompt mode has one fixed posture: every SafetyMode maps to no flag, read_only is best-effort, and
  write/yolo cannot escalate (`write_uses_bypass=True`).
- **Re-verify:** confirm `-p --output-format stream-json` still emits the assistant line + the
  `session.resume_hint` meta line; re-test whether `-p` now accepts a permission flag (would let
  write/yolo escalate).

### OpenHands (`openhands`)
- **Status:** working **with a required env overlay**. `openhands --headless --json -t "<task>"` (JSONL
  `MessageEvent`s interleaved with Rich UI text, which the JSONL parser skips).
- **Tested:** `tests/parsers/openhands/`, `tests/test_openhands_adapter.py`, `RUTHERFORD_IT_OPENHANDS`.
  Auth: a configured LLM (OpenHands Cloud login or a stored LLM key); no non-interactive check, so
  `unknown` + `doctor`.
- **Known issues:** **must run with `PYTHONIOENCODING=utf-8`** (the adapter always sets it, plus
  `PYTHONUTF8=1` and `OPENHANDS_SUPPRESS_BANNER=1`). Without UTF-8 stdio, OpenHands crashes with
  `UnicodeEncodeError` printing a checkmark / wave glyph to a Windows `cp1252` pipe — it never reaches
  the model. `--headless` auto-approves → read_only best-effort (`--llm-approve` gates high-risk
  actions; `--always-approve` for write/yolo). The session id comes from the `--resume <uuid>` hint line
  (the dashed UUID; the adjacent `Conversation ID:` line is dashless and `--resume` rejects it).
- **Re-verify:** confirm `--headless --json` still emits agent `MessageEvent`s with
  `llm_message.content` text; confirm the UTF-8 crash still needs the env overlay (a fixed OpenHands may
  not).

### pi (`pi`)
- **Status:** working. `pi -p --mode json [--tools read,grep,find,ls] "<prompt>"` (verbose JSONL; the
  answer is the last assistant `message_end`).
- **Tested:** `tests/parsers/pi/`, `tests/test_pi_adapter.py`, `RUTHERFORD_IT_PI`. Auth: a configured
  provider, checked with `pi --list-models`. `available_models` parses that table.
- **Known issues:** genuine read-only via the `--tools read,grep,find,ls` allowlist (no edit/write/bash);
  `write`/`yolo` leave the default toolset (which auto-runs in `-p` mode), so `write_uses_bypass=True`.
  pi is bring-your-own-provider (default `google`); on a free account it is often configured with a
  HuggingFace provider. `--thinking` supports every effort tier.
- **Re-verify:** confirm the last assistant `message_end` still carries `content` text parts +
  `usage.cost.total`; confirm the `--tools` names (read/grep/find/ls) are unchanged.

## Per-CLI notes — the pre-existing roster

These are stable and documented in their adapter module docstrings + [integration-testing.md](integration-testing.md);
the standout maintenance item is Antigravity.

- **Antigravity (`agy`)** — the highest-maintenance adapter. `agy -p` emits nothing usable to stdout, so
  the adapter reads the agent's transcript file, whose schema is **community reverse-engineered and
  pinned** (`AntigravityAdapter.verified_version`). `agy` auto-updates, so a bump can change the
  `brain/` transcript layout. An integration test fails when the running version passes the pin —
  re-verify the layout, update `verified_version` + the docstring, and re-pin. read_only is best-effort
  (agy ≥ 1.0.8 print mode applies edits with no deny flag — see the adapter's SAFETY CAVEAT). Watch
  issue #76: if `agy --print` ever carries the answer on stdout, the transcript archaeology can retire
  (a canary integration test signals this).
- **Claude Code, Codex, Cursor, Qwen, OpenCode, Goose, Droid, Vibe, Copilot** — structured-output or
  stdin-based adapters with `check_output_contract` drift canaries. Re-read `--help` after a version
  bump and confirm the JSON/event fields the adapter reads. Codex carries a Windows-sandbox nuance
  (`-c windows.sandbox=unelevated`) and a distinct `RESUME_FAILED` path.
- **Ollama, LM Studio** — optional local models (`optional=True`, excluded from an auto-`all` panel).
  Bring your own model; first call is slow (cold weight load), so a longer `[adapters.<id>] timeout_s`
  is recommended.

## When a CLI breaks

1. **Reproduce headless.** Run the exact invocation the adapter builds (from its module docstring) in a
   throwaway directory and inspect stdout/stderr/exit. The capture harness used to build these adapters
   lives at `.research/2026-06-13-v2-clis/smoke/` for reference.
2. **Classify.** A flag rename / removed mode → fix `build_invocation` + `map_safety`. A changed output
   shape → fix `parse_output` + the golden samples (and `check_output_contract` should already have
   flagged it as `CONTRACT_MISMATCH`). An auth change → fix `check_auth`. A Windows-launch issue →
   suspect the npm-shim `.ps1` path in `runtime/launch.py`.
3. **Re-pin + document.** Update the verified date in the adapter docstring and the supported-CLIs
   tables, note the change here, run `just check` and `just test-integration`.
4. **If it cannot be made to work headless** (an interactive-only TUI with no scriptable mode and no
   capturable output), it fails the hard gate in [adding-a-cli.md](adding-a-cli.md) — disable the
   adapter (`[adapters.<id>] enabled = false`) and record why, rather than ship a broken target.
