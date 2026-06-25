# Claude Code on AWS Bedrock / Google Vertex / enterprise wrappers

If your Claude Code is configured for **AWS Bedrock** (`CLAUDE_CODE_USE_BEDROCK=1`), **Google Vertex**
(`CLAUDE_CODE_USE_VERTEX=1`), or an enterprise distribution that wraps it (for example Amazon's internal
"Toolbox" build), a `claude_code` turn through Rutherford can fail with:

```
ACP turn for claude_code failed: Internal error: API Error (claude-opus-4-8): 400 The provided model
identifier is invalid.. Try --model to switch to us.anthropic.claude-opus-4-1-20250805-v1:0.
```

`doctor connect_only=true` reports `claude_code` as `reachable` (spawn + handshake are fine), and the
standalone `claude` CLI works on the same machine — only the turn through Rutherford fails. This page
explains why, what does **not** fix it, and the configuration that does.

> **TL;DR — the fix.** Pin a valid provider model id for the `claude_code` seat in Rutherford's *own*
> config (global `config.toml`), not in `~/.claude/settings.json`:
>
> ```toml
> [agents.claude_code]
> default_model = "global.anthropic.claude-opus-4-8[1m]"   # your real inference-profile id
>
> [agents.claude_code.env]
> ANTHROPIC_MODEL = "global.anthropic.claude-opus-4-8[1m]"
> ANTHROPIC_CUSTOM_MODEL_OPTION = "global.anthropic.claude-opus-4-8[1m]"
> ```
>
> Replace the id with the one your org uses (the error message's "Try `--model` to switch to …" suggestion
> is a good candidate). Then reconnect the Rutherford MCP server and re-run `doctor agent=claude_code`.

## Why it happens

Rutherford drives `claude_code` through the third-party npm adapter
`@agentclientprotocol/claude-agent-acp`, which uses the `@anthropic-ai/claude-agent-sdk` — the **SDK
path, not the `claude` CLI binary**. On the affected builds (observed in adapter v0.45.0):

1. The adapter loads `~/.claude/settings.json` via the SDK. When that file has `enforceAvailableModels:
   true` with an `availableModels` list of **bare aliases** (`claude-opus-4-8`, `claude-sonnet-4-6`, …),
   the adapter rewrites every model entry's value down to the bare alias.
2. It resolves your `settings.model` (a full Bedrock inference-profile id) by *substring-matching* it back
   to the bare-alias entry, then calls `setModel("claude-opus-4-8")`.
3. The SDK path does **not** re-apply the `modelOverrides` map to a programmatically-set model, so the bare
   alias reaches Bedrock unchanged — and Bedrock rejects it (`400 The provided model identifier is
   invalid`), because it needs an inference-profile id like `us.anthropic.claude-opus-4-1-20250805-v1:0` or
   `global.anthropic.claude-opus-4-8[1m]`.

The standalone `claude` CLI works because it applies the `env` block of `settings.json` to *itself* and
resolves the Bedrock model on its own; the SDK/adapter path does not.

**The enterprise-wrapper twist.** A managed distribution (e.g. Amazon Toolbox) may rewrite
`availableModels` back to the approved bare aliases on *every* `claude` launch, in *any* `CLAUDE_CONFIG_DIR`
— so editing `settings.json` on disk is futile; the wrapper reverts it. What survives the wrapper is the
**subprocess environment** and files **outside the `.claude` tree** — which is exactly where Rutherford's
own config lives.

## Approaches that do NOT work

| Attempt | Why it fails |
| --- | --- |
| Rutherford `[agents.claude_code] default_model = "<bedrock id>"` *alone* | The model is selected over ACP only when the adapter *advertises* that exact value. The adapter advertises bare aliases, so a raw id is not selectable, and the value is clobbered by the allowlist rewrite. |
| `CLAUDE_MODEL_CONFIG` in `settings.json` `env` | Substring-matched back to the bare alias, same as `settings.model`. |
| Editing `availableModels` in `settings.json` (even in a separate `CLAUDE_CONFIG_DIR`) | The enterprise wrapper reverts it on every launch. |
| Relying on Rutherford's automatic `ANTHROPIC_MODEL` injection | On a build with `enforceAvailableModels`, the injected `ANTHROPIC_MODEL` is substring-matched back to the bare alias before `setModel`, so it is rewritten away. (Rutherford still injects it — it is enough on a *non-enforced* Bedrock build — but it is not sufficient under an enforced allowlist.) |

## The working fix

Set the model id in Rutherford's own config for the `claude_code` seat only:

```toml
[agents.claude_code]
default_model = "global.anthropic.claude-opus-4-8[1m]"

[agents.claude_code.env]
ANTHROPIC_MODEL = "global.anthropic.claude-opus-4-8[1m]"
ANTHROPIC_CUSTOM_MODEL_OPTION = "global.anthropic.claude-opus-4-8[1m]"
```

Why this works:

- **`[agents.<id>.env]` is applied to that subprocess only**, layered on top of the inherited environment
  (see [configuration.md](configuration.md)). It lives in Rutherford's config, outside the `.claude` tree,
  so the enterprise wrapper that rewrites `settings.json` never touches it.
- **`ANTHROPIC_CUSTOM_MODEL_OPTION` is exempt from the allowlist rewrite** (the adapter appends its value
  intact, per the adapter's own comments). That is the value that actually survives `enforceAvailableModels`
  and reaches Bedrock as a real inference-profile id — `ANTHROPIC_MODEL` alone is rewritten back to the
  bare alias.
- The org-managed `settings.json` is never touched, so this does not fight the wrapper or change org policy
  for the standalone CLI.

Where the global `config.toml` lives:

| Platform | Path |
| --- | --- |
| Windows | `%APPDATA%\rutherford\config.toml` |
| Linux / macOS | `$XDG_CONFIG_HOME/rutherford/config.toml` (fallback `~/.config/rutherford/config.toml`) |

Config is read once at server start, so **reconnect the MCP server** after editing it, then verify:

```
doctor agent=claude_code   ->   status: ok
```

`setup` detects a Bedrock/Vertex host and scaffolds a commented version of this block into the starter
`config.toml`, and `doctor` attaches a `remediation_hint` pointing here when it sees the rejection signature
on a Claude Code seat.

## What Rutherford does and does not do

This class of problem is **not a bug in Rutherford**, and it is not fixable in the adapter or the wrapper
(both are third-party). Rutherford's role is **detection, documentation, and config ergonomics**:

- `doctor` classifies the rejection as `model_unavailable` (the seat is reachable; the model/provider config
  is wrong) and attaches a `remediation_hint` describing the per-agent `env` fix.
- `setup` surfaces the commented `[agents.claude_code.env]` block on a detected Bedrock/Vertex host.
- Rutherford does **not** auto-inject `ANTHROPIC_CUSTOM_MODEL_OPTION` — silently bypassing an org's enforced
  model allowlist is a policy decision, so it is left as an explicit, user-authored opt-in.

## See also

- [configuration.md](configuration.md) — the `[agents.<id>.env]` block and the full `AgentConfig` reference.
- [troubleshooting.md](troubleshooting.md) — other `doctor` failure modes.
