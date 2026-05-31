# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/chapmanjw/rutherford-mcp-server/security/advisories/new)
rather than a public issue. You will receive an acknowledgement within a few days.

## Security model

Rutherford spawns other coding agents as subprocesses on the host. Treat that capability with
the same care as a shell.

- **Safe by default.** Every delegation runs in `read_only` mode unless the caller explicitly
  opts into `write` or `yolo`. Review and consensus uses are read-only by nature. No adapter
  ever defaults to its permission-bypass flag.
- **Trusted-workspace gate.** `write` and `yolo` require both an explicit safety argument and a
  trusted-workspace check — an allowlist of paths or a per-call confirmation flag. A delegation
  cannot mutate an arbitrary directory by accident.
- **Argument arrays, never shell strings.** Invocations are always built as an argv list and
  passed to the process layer without a shell, so a prompt or path cannot inject a command.
- **Bounded recursion.** A delegation depth is tracked on every request and propagated to
  spawned children through `RUTHERFORD_DEPTH`. Rutherford refuses to spawn beyond a configurable
  maximum depth, and caps the number of targets per call, so a CLI-calls-itself chain cannot
  fan out without bound.
- **No interactive logins.** Rutherford detects auth state but never performs a login on the
  user's behalf and never handles a credential value. Each CLI authenticates through its own
  mechanism (an API-key environment variable or a pre-existing session). `doctor` reports an
  unauthenticated target rather than hanging on a prompt.
- **Process-tree termination.** Every run has a timeout and kills its whole process tree on
  expiry or cancellation, so a runaway agent does not linger.

## Secrets

Rutherford reads API keys only from the environment, by name, to detect auth state. It never
prints a key, writes one to disk, or includes one in a `DelegationResult`. Keep credentials in
environment variables or each CLI's own credential store, never in the repository.

## Scope

Delegating in `write` or `yolo` mode grants a subprocess the ability to modify files in the
target workspace. Only enable those modes for directories and CLIs you trust.
