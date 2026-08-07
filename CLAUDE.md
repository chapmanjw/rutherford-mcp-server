# CLAUDE.md

Claude Code reads `CLAUDE.md` and does not read `AGENTS.md`, so this file exists to point at the one
that does the work. [AGENTS.md](AGENTS.md) is canonical for every agent; keep instructions there, not
here, so there is one copy to keep true.

The line below is an import, not a link. Leave it in exactly this case: it resolves on a
case-insensitive filesystem either way, and only fails on Linux and macOS, which is the worst place to
learn about it. A missing import target is silent — no error, no warning, just a session with no
project context at all — so `tests/test_agent_docs.py` fails the build if this ever stops resolving.

@AGENTS.md
