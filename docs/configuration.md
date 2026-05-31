# Rutherford configuration reference

Rutherford uses TOML for configuration. The loader discovers, merges, and validates config at
startup. A missing file is not an error; defaults apply. Invalid config (parse failure or
schema violation) raises `ConfigError` and the process exits non-zero before serving any
requests.

## Discovery and precedence

When `RUTHERFORD_CONFIG` is set, that single file is used and discovery is skipped. If the
file does not exist, startup fails immediately.

Without `RUTHERFORD_CONFIG`, the loader reads two files and merges them:

```
global config file     (lowest priority)
        +
project-local file     (overlays global; project wins on any key present in both)
        +
RUTHERFORD_* env vars  (highest priority; override specific scalar fields)
```

Nested dicts merge recursively. List and scalar values replace entirely (no append
semantics).

### Global config path

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\rutherford\config.toml` |
| Linux / macOS | `$XDG_CONFIG_HOME/rutherford/config.toml` (fallback: `~/.config/rutherford/config.toml`) |

### Project-local config

The loader searches the current working directory for `rutherford.toml`, then
`.rutherford.toml`. The first match wins; both are not read.

### Selecting a single file

```
RUTHERFORD_CONFIG=/path/to/my-config.toml
```

This disables global and project-local discovery entirely.

---

## `RutherfordConfig` fields

All fields are optional. Unset fields take the listed default.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled_adapters` | `list[str]` or omitted | all built-ins + all generic adapters | Restrict the registry to these adapter ids. Unknown ids in this list are a startup error. |
| `adapters` | `dict[str, AdapterConfig]` | `{}` | Per-adapter overrides keyed by adapter id. |
| `generic_adapters` | `list[GenericAdapterConfig]` | `[]` | Config-defined adapters with no code module. |
| `default_safety_mode` | `string` | `"read_only"` | Safety posture applied when a caller omits the field. One of `read_only`, `propose`, `write`, `yolo`. |
| `default_timeout_s` | `float` | `300.0` | Per-run timeout in seconds. |
| `role_dirs` | `list[str]` | `[]` | Extra directories to search for role markdown files. Built-in roles always load. |
| `max_depth` | `int` | `3` | Maximum delegation depth. Delegations at this depth are refused. |
| `max_targets` | `int` | `8` | Maximum targets per consensus call. |
| `trusted_workspaces` | `list[str]` | `[]` | Absolute paths under which `write` and `yolo` delegations are permitted. |
| `synthesize_default` | `bool` | `false` | Whether consensus synthesizes server-side by default. |

### `AdapterConfig` fields (under `[adapters.<id>]`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `true` | Set to `false` to remove this adapter from the registry at startup. |
| `default_model` | `string` or omitted | none | Model string passed when the caller does not specify one. |

---

## Environment overrides

These override specific fields after the config files are merged. They do not replace the
entire config.

| Variable | Type | Overrides |
|----------|------|-----------|
| `RUTHERFORD_CONFIG` | path | Replaces file discovery; must point to an existing file. |
| `RUTHERFORD_MAX_DEPTH` | integer | `max_depth` |
| `RUTHERFORD_MAX_TARGETS` | integer | `max_targets` |
| `RUTHERFORD_DEFAULT_TIMEOUT_S` | float | `default_timeout_s` |
| `RUTHERFORD_DEFAULT_SAFETY` | string | `default_safety_mode` |
| `RUTHERFORD_TRUSTED_WORKSPACES` | `os.pathsep`-delimited paths | `trusted_workspaces` |
| `RUTHERFORD_ROLE_DIRS` | `os.pathsep`-delimited paths | `role_dirs` |
| `RUTHERFORD_DEPTH` | integer | Set automatically on child processes to track delegation depth. Do not set this yourself. |

---

## Config-defined generic adapters

A generic adapter drives a CLI entirely from config. Use it for CLIs with clean headless
invocations and deterministic stdout. CLIs that need custom output parsing (streaming events,
transcript files) still require a code adapter.

The argv is assembled in this order:

```
[binary, *base_args, *safety_args, *model_args, *working_dir_args, *extra_args, <prompt>]
```

The prompt is the final positional argument unless `prompt_on_stdin` is true, in which case
it is written to stdin and omitted from the argv.

### `GenericAdapterConfig` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `string` | required | Unique adapter id. Collides with a built-in? Replaces it. |
| `display_name` | `string` | required | Human-readable label. |
| `binary` | `string` | required | Executable name or absolute path. |
| `base_args` | `list[str]` | `[]` | Arguments placed immediately after the binary. |
| `prompt_on_stdin` | `bool` | `false` | Send the prompt on stdin instead of as a positional argument. |
| `model_flag` | `string` or omitted | none | Flag prefix for model selection (e.g. `"--model"`). Omit if the CLI has no model flag. |
| `working_dir_flag` | `string` or omitted | none | Flag prefix for working directory (e.g. `"--dir"`). Omit if the CLI uses process cwd only. |
| `extra_args` | `list[str]` | `[]` | Arguments appended after safety/model/working-dir args, before the prompt. |
| `output_mode` | `string` | `"text"` | How to extract the answer. One of `text`, `json`, `jsonl`, `transcript`. |
| `json_text_path` | `string` or omitted | none | Dotted key path into the parsed JSON object (e.g. `"message.content"`). Only used when `output_mode` is `json`. Omit to return the full JSON object as text. |
| `safety` | `GenericSafetyConfig` | all empty lists | Per-safety-mode argv fragments. |
| `version_args` | `list[str]` | `["--version"]` | Args passed to probe the binary version. |
| `static_models` | `list[str]` | `[]` | Hard-coded model list reported by the adapter (no runtime query). |
| `auth_env` | `list[str]` | `[]` | Environment variable names whose presence signals authentication. |
| `runtime` | `string` | `"native"` | Where the binary runs. One of `native`, `wsl_interop`. |

### `GenericSafetyConfig` fields (under `safety`)

Each field is a list of argv fragments injected when that safety mode is active.

| Field | Type | Default |
|-------|------|---------|
| `read_only` | `list[str]` | `[]` |
| `propose` | `list[str]` | `[]` |
| `write` | `list[str]` | `[]` |
| `yolo` | `list[str]` | `[]` |

---

## Complete example

```toml
# rutherford.toml

# Restrict to three adapters instead of loading all built-ins.
enabled_adapters = ["claude_code", "codex", "my_internal_tool"]

default_safety_mode = "read_only"
default_timeout_s   = 120.0
max_depth           = 2
max_targets         = 4
synthesize_default  = false

# Paths under which write/yolo are allowed.
trusted_workspaces = [
    "/home/user/projects/myapp",
    "/tmp/sandbox",
]

# Extra directories to search for role markdown files.
role_dirs = ["/home/user/.config/rutherford/roles"]

# Per-adapter overrides.
[adapters.claude_code]
default_model = "claude-sonnet-4-6"

[adapters.codex]
enabled = false   # disable without removing from enabled_adapters

# A config-defined generic adapter -- no code module required.
[[generic_adapters]]
id           = "my_internal_tool"
display_name = "Internal Coding Tool"
binary       = "internal-tool"
base_args    = ["--headless", "--json"]
output_mode  = "json"
json_text_path = "result.text"
model_flag   = "--model"
working_dir_flag = "--dir"
prompt_on_stdin = false
extra_args   = ["--no-color"]
version_args = ["--version"]
static_models = ["fast", "powerful"]
auth_env     = ["INTERNAL_TOOL_API_KEY"]
runtime      = "native"

[generic_adapters.safety]
read_only = ["--read-only"]
propose   = ["--propose"]
write     = []
yolo      = ["--skip-approvals"]
```

---

## Per-CLI authentication

Rutherford never performs interactive logins. It detects auth state only. Set the relevant
environment variables before starting the server, or run each CLI's interactive login once to
establish a reusable session.

See `.env.example` in the repo root for the full list of per-CLI auth variables
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `KIRO_API_KEY`, Goose provider vars, etc.) and notes
on CLIs that use OS credential stores instead of env vars (Antigravity authenticates via
Google OAuth and has no API-key env var).
