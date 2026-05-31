# Architecture

Rutherford is a stdio MCP server that runs other agentic coding CLIs as headless subprocesses.
Its two operations are **delegate** (hand one task to one CLI) and **consensus** (hand the same
task to several CLIs in parallel and collect every answer). It never calls a model provider API
directly and never reimplements a CLI's own features.

**Non-goals.** Rutherford does not manage CLI authentication, does not store conversation history
itself, does not stream tokens to the MCP client, and does not implement any coding agent behavior.
It is a transport and orchestration layer, not an agent.

---

## Layer diagram

```
MCP tool layer     src/rutherford/server.py + tools/
                   FastMCP @mcp.tool wrappers; validates input, calls a service,
                   returns toolSuccess / toolError; no orchestration logic here.
        |
services           src/rutherford/services/
                   delegation.py  -- single-target, sync/async, guards
                   consensus.py   -- fan-out, stance steering, optional synthesis
                   jobs.py        -- background job store
                   roles.py       -- role preamble loader and store
        |
adapters           src/rutherford/adapters/
                   base.py        -- CLIAdapter Protocol + BaseCLIAdapter
                   registry.py    -- closed id -> adapter mapping
                   claude_code.py, codex.py, opencode.py, goose.py,
                   kiro.py, antigravity.py, generic.py
        |
runtime            src/rutherford/runtime/
                   process.py     -- ProcessRunner Protocol + AsyncProcessRunner
                   probe.py       -- CommandProbe Protocol + SystemProbe
                   launch.py      -- cross-platform argv preparation
                   depth.py       -- depth guard + target cap
                   platform.py    -- WSL detection
                   paths.py       -- path translation
        |
domain + config    src/rutherford/domain/   models, enums, errors, error_codes
                   src/rutherford/config/   schema, loader
                   src/rutherford/io/       serialize.py (TOON seam)
```

Dependencies point inward. The domain layer imports nothing from any other layer. Adapters import
from domain, runtime, and config but not from services. Services import from adapters (via the
`AdapterRegistry` interface), runtime, and domain. The tool layer imports only from services and
domain. Nothing in the core imports a concrete adapter by class name; all adapter access goes
through the registry.

---

## The two interfaces

### CLIAdapter (`adapters/base.py`)

`CLIAdapter` is a `Protocol`. The core depends only on this interface; no concrete adapter class
is imported anywhere outside `registry.py`. The contract has two load-bearing methods:

- `build_invocation(req, ctx) -> InvocationSpec` -- pure function. Given a normalized
  `DelegationRequest` and an `InvocationContext`, returns an `InvocationSpec` with an `argv`
  list, an `env` overlay, a `cwd`, and a `runtime` hint. It never builds a shell string.
  Role preamble injection is the adapter's responsibility: use a native system-prompt flag where
  the CLI has one, or call `_compose_prompt` to prepend it to the prompt.

- `parse_output(raw, ctx) -> DelegationResult` -- maps the raw `ProcessResult` to the normalized
  envelope, including on non-zero exit. All CLI-specific quirks live here and must not leak
  upward. The Antigravity adapter's transcript read is the canonical example; see the section
  on CLI quirks below.

The other methods (`detect`, `check_auth`, `available_models`, `capabilities`, `map_safety`) are
used by the `capabilities` and `doctor` tools and by `DelegationService` for the binary-present
check and safety mapping.

`BaseCLIAdapter` is an abstract base class that provides default implementations of `detect`
(via an injected `CommandProbe`) and `available_models` (from a static model tuple), plus small
helpers (`_detect_version`, `_with_files`, `_compose_prompt`, `_auth_from_env_or_command`).
Concrete adapters implement only what is genuinely CLI-specific.

**Why it exists.** The `CLIAdapter` interface is what makes the core testable: every service
takes an `AdapterRegistry` (not any concrete adapter), so unit tests use `FakeAdapter` with no
real CLI subprocess. Adding a new CLI is additive and requires no changes to services or tools.

### ProcessRunner (`runtime/process.py`)

`ProcessRunner` is a `Protocol` with one async method: `run(spec, timeout_s, on_progress)`.

The real implementation, `AsyncProcessRunner`, uses `asyncio.create_subprocess_exec` (never a
shell), feeds stdin, streams stderr lines to `on_progress` as they arrive, enforces the timeout
with `asyncio.wait_for`, and on timeout or cancellation kills the entire process tree with
`psutil` (agents spawn child processes; killing only the direct child would orphan them).

`CommandProbe` is a separate, synchronous interface for the short metadata calls (`which`,
`--version`, auth status checks). `SystemProbe` backs it with `subprocess.run`. These metadata
calls are distinct from the orchestration path and never mutate.

**Why two runner types.** `ProcessRunner` is async and handles the full lifecycle for long-running
agent invocations. `CommandProbe` is synchronous and used only for brief, read-only metadata
queries. Both are injected, so neither the service layer nor any adapter depends on a real
subprocess in tests.

---

## Request/result flow: a single delegate call

```
MCP client calls delegate(cli="claude_code", prompt="...", safety_mode="read_only")
        |
        v
delegate_tool (tools/delegate.py)
  Build DelegationRequest(target=Target(cli, model), ...)
        |
        v
DelegationService.delegate (services/delegation.py)
  1. registry.get(cli)          -> CLIAdapter  (RegistryError if unknown)
  2. ensure_within_depth(...)   -> raises DepthLimitError if depth >= max_depth
  3. adapter.detect()           -> fail if binary absent
  4. is_mutating(safety_mode)   -> trusted-workspace gate for write/yolo
  5. roles.get(role)            -> inject role_preamble into InvocationContext
  6. adapter.build_invocation(req, ctx)  -> InvocationSpec (pure, argv list)
  7. spec.env overlay: RUTHERFORD_DEPTH = depth + 1
  8. runner.run(spec, timeout)  -> ProcessResult
  9. adapter.parse_output(raw, ctx)  -> DelegationResult
        |
        v
tool_success(result)   ->   encode(result)   ->   TOON text block returned to MCP client
```

Operational failures at any step (unknown id, binary absent, timeout, non-zero exit, parse
failure) become a structured `DelegationResult(ok=False, error=ErrorInfo(code, message))` rather
than an exception, so a consensus panel never aborts on one bad voice.

---

## The DelegationResult envelope

Every adapter's `parse_output` must return a `DelegationResult` regardless of outcome.

| Field | Type | Notes |
|---|---|---|
| `target` | `Target` | The `(cli, model)` pair that was delegated to |
| `ok` | `bool` | `True` on success, `False` on any failure |
| `exit_code` | `int \| None` | Raw process exit code; `None` on timeout |
| `text` | `str` | The clean final answer (empty on failure) |
| `raw` | `str \| None` | Combined stdout+stderr; only when `include_raw=True` |
| `artifacts` | `list[Artifact]` | Files the agent reported changing |
| `duration_s` | `float` | Wall-clock time in seconds |
| `session_id` | `str \| None` | Opaque; round-trips to the CLI's resume mechanism |
| `cost` | `Cost \| None` | Token counts and USD cost, where the CLI reports them |
| `error` | `ErrorInfo \| None` | Structured error on failure; `None` on success |
| `safety_mode` | `SafetyMode` | Echoes the mode that was used |

`ErrorInfo` carries a `code` from `domain/error_codes.py` (stable `StrEnum` members, never
renamed), a human `message`, and an optional `details` dict. Clients may switch on `error.code`.

### toolSuccess / toolError and the TOON seam

The tool layer calls `tool_success(data)` or `tool_error(code, message)` from `context.py`.
Both funnel through `io/serialize.py:encode()`, which converts pydantic models to plain data
(`model_dump(mode="json", exclude_none=True)`) and passes the result to the `toon` encoder.
This is the single swap point for the serialization format: changing the encoder is a one-line
change in `encode()` and has no effect on anything above or below it.

The `toon` package used is `python-toon >= 0.1.3` (imported as `from toon import encode`).
The PyPI packages `toon-format` and `toon-encoder` are not used.

---

## The adapter registry

`adapters/registry.py` defines `AdapterRegistry`, an immutable `id -> CLIAdapter` mapping.

The built-in adapters are listed in `BUILTIN_ADAPTERS` as `(id, module_path, class_name)` tuples.
The registry imports each class lazily via `importlib.import_module`, so loading the registry
carries no import-time dependency on any concrete adapter. Adding a built-in adapter is a
one-line addition to `BUILTIN_ADAPTERS`.

`build_registry(config)` assembles the final mapping:

1. Load enabled built-ins (skipping any with `adapters.<id>.enabled = false` in config).
2. Instantiate any `generic_adapters` entries as `GenericAdapter` instances.
3. If `enabled_adapters` is set, restrict to those ids and raise `RegistryError` on any unknown id.

A duplicate adapter id or an unknown id in `enabled_adapters` raises `RegistryError` at startup
(fail-fast). Looking up an unknown id during a tool call also raises `RegistryError`, which
`DelegationService` converts to a structured failure result.

**The config-driven generic adapter.** `adapters/generic.py:GenericAdapter` is driven entirely
by a `GenericAdapterConfig` entry in the config file. It handles CLIs with a clean headless
invocation and deterministic stdout (plain text or a JSON object). The `argv` is assembled as
`[binary, *base_args, *safety_args, *model_args, *working_dir_args, *extra_args, prompt]` (or
with prompt on stdin). CLIs whose output requires custom parsing (streaming events, transcript
files) still need a code adapter.

See [docs/adding-a-cli.md](adding-a-cli.md) for the decision tree and step-by-step instructions.

---

## CLI quirks: parse_output

All CLI-specific output parsing is contained in each adapter's `parse_output`. Nothing leaks up.

The current quirks, by adapter:

| Adapter | Binary | parse_output notes |
|---|---|---|
| `claude_code` | `claude` | Reads the last line of stdout as a JSON object; fields: `result`, `session_id`, `is_error`, `total_cost_usd`, `usage` |
| `codex` | `codex` | JSONL event stream; final answer is the last `item.completed` event with `item.details.type == "agent_message"`; session from `thread_id` in `thread.started` |
| `opencode` | `opencode` | NDJSON stream with `-q`; final text from last `{"type":"text"}` part |
| `goose` | `goose` | `--output-format json` emits a single JSON object; schema is not formally versioned -- parse defensively |
| `kiro` | `kiro-cli` | Plain markdown to stdout; no JSON mode for the answer |
| `antigravity` | `agy` | stdout is unreliable; `parse_output` reads the transcript file (see below) |
| `generic` | configurable | Plain text (default) or last JSON object on stdout, with optional dotted `json_text_path` extraction |

### Antigravity transcript quirk

`agy -p` does not reliably write the agent's final answer to stdout. `AntigravityAdapter.parse_output`
reads it from a JSONL transcript file instead. The lookup sequence:

1. Read `~/.gemini/antigravity-cli/cache/last_conversations.json` to map the working directory to
   a conversation id.
2. If the index misses, fall back to the most recently modified subdirectory of
   `~/.gemini/antigravity-cli/brain/`.
3. Read `brain/<conv-id>/.system_generated/logs/transcript.jsonl` and extract the last line with
   `source == "MODEL"`, `status == "DONE"`, `type == "PLANNER_RESPONSE"`, and non-empty content.

The transcript schema is community-reverse-engineered (verified against agy 1.0.2, 2026-05-30).
Treat a schema change as a `TRANSCRIPT_NOT_FOUND` parse failure. Pin the agy version.

---

## Depth guard and target cap

A CLI delegated to another CLI via the same Rutherford server creates a chain. To prevent
unbounded recursion, `runtime/depth.py` tracks delegation depth across process boundaries:

- `RUTHERFORD_DEPTH` environment variable carries the current depth to child processes.
- When the server starts, `current_depth()` reads this variable to set `AppContext.base_depth`.
- Before each delegation, `DelegationService` overlays `RUTHERFORD_DEPTH = depth + 1` on the
  child's environment via `child_depth_env()`.
- `ensure_within_depth(depth, max_depth)` refuses to spawn if `depth >= max_depth`. The default
  `max_depth` is 3 (configurable in `RutherfordConfig`).

`ensure_within_target_cap(count, max_targets)` caps the number of targets in a single consensus
call. The default `max_targets` is 8.

Both raise typed exceptions (`DepthLimitError`, `RutherfordError`) that `DelegationService`
converts to structured failure results with stable error codes (`MAX_DEPTH_EXCEEDED`,
`TOO_MANY_TARGETS`).

---

## Roles

Roles are named personas that inject a system prompt preamble into a delegation. The built-in
roles ship inside the package at `src/rutherford/roles/`:

| Name | File |
|---|---|
| `codereviewer` | `roles/codereviewer.md` |
| `debugger` | `roles/debugger.md` |
| `planner` | `roles/planner.md` |
| `security` | `roles/security.md` |

Each file has a simple `key: value` frontmatter block (`name`, `display_name`, `description`)
and a markdown body that becomes the `preamble`. Built-in roles are loaded via
`importlib.resources` and work in all install modes (editable, wheel, zipapp). Additional role
directories can be specified in `RutherfordConfig.role_dirs`; files there override built-ins by
name. Editing a role never requires a code change.

`DelegationService` resolves the role preamble and sets it on `InvocationContext.role_preamble`.
The adapter's `build_invocation` injects it: via the CLI's native system-prompt flag where one
exists, or by prepending to the prompt via `BaseCLIAdapter._compose_prompt`. The service does not
double-inject it.

---

## Safety modes and the trusted-workspace gate

`SafetyMode` has four levels (all are `StrEnum` values that serialize cleanly):

| Value | Meaning |
|---|---|
| `read_only` | Agent must not modify the workspace (default) |
| `propose` | Agent may propose changes (e.g. a diff) but not apply them |
| `write` | Agent may modify the workspace under the CLI's normal approval flow |
| `yolo` | Agent may act without approval prompts (the CLI's bypass mode) |

`write` and `yolo` are mutating. A mutating delegation requires either `trust_workspace=True` on
the request or the `working_dir` to be at or under a path in `RutherfordConfig.trusted_workspaces`.
Failing this check returns `WORKSPACE_NOT_TRUSTED`. No adapter ever defaults to its bypass flag;
`SafetyMode.READ_ONLY` is the default everywhere.

`adapter.map_safety(mode)` translates the universal mode to a `SafetyFlags(args, env, note)`
specific to that CLI. Every adapter must return a value for every mode.

---

## Cross-references

- Configuration options and file locations: [docs/configuration.md](configuration.md)
- Adding a new CLI (decision tree, code adapter vs. generic): [docs/adding-a-cli.md](adding-a-cli.md)
- Trusted-workspace policy, secret handling, argv rules: [docs/security.md](security.md)
