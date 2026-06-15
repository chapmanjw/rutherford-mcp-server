# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/chapmanjw/rutherford-mcp-server/security/advisories/new)
rather than a public issue. You will receive an acknowledgement within a few days.

## Security model

Rutherford spawns other coding agents as ACP servers on the host and acts as the permission
authority for what they do over the protocol. Treat that capability with the same care as a shell.

- **Safe by default.** Every delegation runs in `read_only` mode unless the caller explicitly
  opts into `write` or `yolo`. Rutherford answers the agent's filesystem-write, terminal, and
  tool-permission requests according to the mode, so a non-mutating mode denies them at the source.
- **Trusted-workspace gate.** `write` and `yolo` require both an explicit safety argument and a
  trusted-workspace check — an allowlist of paths or a per-call confirmation flag. A delegation
  cannot mutate an arbitrary directory by accident.
- **Clean stdio, never a shell string.** Agents are launched as an argv array without a shell, so
  a prompt or path cannot inject a command. On Windows, npm shims are resolved to their real target
  so the raw JSON-RPC stdin the ACP transport needs is never corrupted.
- **Bounded recursion.** A delegation depth is tracked on every request and propagated to spawned
  children. Rutherford refuses to spawn beyond a configurable maximum depth, and caps the number of
  targets per call, so a calls-itself chain cannot fan out without bound.
- **No interactive logins.** Rutherford never performs a login on the user's behalf and never
  handles a credential value. Each agent authenticates through its own mechanism (an API-key
  environment variable or a pre-existing session). `doctor` drives a real read-only round trip and
  reports whether an agent actually answers, rather than hanging on a prompt.
- **Process-tree teardown.** Every turn has a timeout and a closing session reaps the agent's
  orphaned descendant process tree, so a runaway or wrapper-spawned CLI does not linger.

## Secrets

Rutherford does not handle a credential value. An agent subprocess inherits the environment so its
own credential discovery works; Rutherford never prints a key, writes one to disk, or includes one
in a `DelegationResult`. Keep credentials in environment variables or each agent's own credential
store, never in the repository.

## Scope

Delegating in `write` or `yolo` mode grants a subprocess the ability to modify files in the
target workspace. Only enable those modes for directories and CLIs you trust.
