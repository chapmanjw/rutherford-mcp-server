# Adding a CLI

This is the contract for adding a new CLI target to Rutherford. Follow it and a contributor (or a
future you) can add a CLI without reading the whole codebase. Keep this doc in sync with the code.

## 1. Hard gate: what a CLI must offer

A CLI can be adapted only if it provides all of the following. If any is missing, it cannot be a
Rutherford target.

1. A non-interactive, headless invocation: one prompt in, a result out, then exit. An
   interactive-only TUI is not adaptable.
2. A way to run without interactive approval prompts, or a flag to pre-grant tool permissions. A
   spawned subprocess cannot answer prompts.
3. Capturable output: deterministic stdout, or a file Rutherford can read when stdout is
   unreliable (the Antigravity case).
4. Non-interactive auth: an API-key environment variable, or a pre-existing persisted session.
   Interactive browser login at call time is not supported; `doctor` must report that state
   rather than hang.

Optional but supported, and worth wiring when present: a model-selection flag, a working-directory
flag, a file-context mechanism, a resume/session mechanism, and a list-models command.

## 2. Config-only adapter vs code adapter

Prefer the config-driven generic adapter. If the CLI has clean stdout (plain text or a single JSON
object) and standard flags, add it as a `GenericAdapterConfig` entry under `generic_adapters` in
config (see [configuration.md](configuration.md)) -- argv template, model/working-dir flags, output
mode, and an optional dotted `json_text_path`. No code.

Write a code adapter only when output parsing or auth needs custom handling: a streaming event
format, a transcript file, or a credential-store probe. This mirrors the registry's closed-mapping
philosophy -- adding a member should be adding an entry, not special-casing logic.

## 3. Interface to implement (code adapters)

Subclass `BaseCLIAdapter` (`src/rutherford/adapters/base.py`) and implement the `CLIAdapter`
interface. Set the class attributes `id`, `display_name`, `binary`, and optionally `static_models`
/ `version_args`. `BaseCLIAdapter` provides `detect`, a default `available_models`, and helpers
(`_detect_version`, `_with_files`, `_compose_prompt`, `_env_present`, `_auth_from_env_or_command`).
Use the envelope builders in `src/rutherford/adapters/results.py`
(`success_result`, `error_result`, `timeout_result`, `nonzero_result`). Register the adapter by
adding one `(id, module, class)` row to `BUILTIN_ADAPTERS` in `src/rutherford/adapters/registry.py`.

Rules:

- `build_invocation(req, ctx)` returns an `InvocationSpec` (argv list, env, cwd, runtime hint, and
  optional stdin). It must be a pure function of its inputs and must never build or return a shell
  string. Call `self.map_safety(ctx.safety_mode)` and append its args; overlay its env. Incorporate
  `ctx.role_preamble` via the CLI's system-prompt flag where one exists, else `_compose_prompt`;
  incorporate `req.files` via the CLI's file flag where one exists, else `_with_files`. Do not set
  `RUTHERFORD_DEPTH` -- the delegation service overlays it.
- `map_safety(mode)` must handle every `SafetyMode` value and default conservatively. Never default
  to a bypass flag.
- `parse_output(raw, ctx)` must return the normalized `DelegationResult` envelope, including on
  timeout (`timeout_result`) and non-zero exit (`nonzero_result`). It is the one place a
  CLI-specific quirk lives (for example reading a transcript file); nothing leaks upward. It must
  not raise.
- `detect`, `check_auth`, and `available_models` must never trigger an interactive login or a
  destructive action. They take their command runner from the injected `CommandProbe`, so they are
  testable with a fake.

## 4. Safety classification

State in both the adapter and this doc how each invocation maps to `SafetyMode`. Review and
consensus uses are read-only. Delegation is read-only in `read_only` and `propose`, and only mutates
in `write` and `yolo`. This is the same read-only-vs-mutating classification the owner's other
servers apply to their tools.

## 5. Testing requirements (the merge bar)

A new adapter is not done until all of these exist and pass:

1. Golden output samples committed under `tests/parsers/<id>/` -- at least one success and one
   error or non-zero-exit sample -- with a `parse_output` golden test asserting the normalized
   envelope (mirror `tests/test_claude_adapter.py`).
2. Unit tests for `detect`, `check_auth`, and `available_models` using `FakeProbe`, with no live
   CLI required.
3. The shared contract test (`tests/test_contract.py`) passes for the new adapter: it satisfies the
   interface, `build_invocation` returns an argv list and never a shell string and is pure, and
   `map_safety` covers every `SafetyMode`. The test discovers adapters from the registry, so a
   registered adapter is covered automatically.
4. An optional live integration test under `tests/integration/`, gated behind `RUTHERFORD_IT_<CLI>`
   and skipped in CI, that runs the real CLI when installed and authenticated.
5. A row in the supported-CLIs table (in this doc and the README) recording the verified invocation,
   the auth method, and the date the flags were last verified against the CLI's `--help`.

Run `just check` (lint, format, license header, mypy strict, unit tests with the coverage floor).

## 6. Checklist

- [ ] Hard gate confirmed (headless, no-prompt, capturable output, non-interactive auth).
- [ ] Config-only vs code decision recorded.
- [ ] Interface implemented (code) or `GenericAdapterConfig` entry added (config).
- [ ] Registered: a row in `BUILTIN_ADAPTERS` (code adapters).
- [ ] Safety mapping defined for all four `SafetyMode` values.
- [ ] Golden samples added under `tests/parsers/<id>/` (success + error/non-zero).
- [ ] Unit tests for `detect`/`check_auth`/`available_models` with `FakeProbe`.
- [ ] Contract test passes (automatic once registered).
- [ ] Integration test added and gated behind `RUTHERFORD_IT_<CLI>`.
- [ ] Docs and the supported-CLIs table updated with the verification date.

## Supported CLIs

| CLI | Adapter id | Headless invocation | Auth | Verified |
| --- | --- | --- | --- | --- |
| Claude Code | `claude_code` | `claude -p "<prompt>" --output-format json` | subscription/OAuth or `ANTHROPIC_API_KEY` | 2026-05-30 |
| Codex | `codex` | `codex exec --json --skip-git-repo-check` (prompt on stdin) | ChatGPT login or `OPENAI_API_KEY` | 2026-05-30 |
| Antigravity | `antigravity` | `agy -p "<prompt>"` (transcript file) | OS credential store (Google) | 2026-05-30 |
| Kiro | `kiro` | `kiro-cli chat --no-interactive "<prompt>"` | `KIRO_API_KEY` or `kiro-cli login` | 2026-05-30 |
| OpenCode | `opencode` | `opencode run --format json -q "<prompt>"` | provider key or `opencode auth login` | 2026-05-30 (docs) |
| Goose | `goose` | `goose run -q -t "<prompt>" --no-session` | `GOOSE_PROVIDER` + provider key | 2026-05-30 (docs) |
