# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Detect-only discovery: match registry agents against what is ALREADY installed, then probe them.

The registry (:mod:`rutherford.acp.registry`) says how to *download* each agent; this module finds the
ones a user already has. For every registry agent it resolves the candidate bin names against PATH and a
curated set of install dirs (``~/.local/bin``, ``~/.cargo/bin``, and ``~/.<vendor>/bin`` -- where many CLIs
land off PATH, e.g. Qoder at ``~/.qoder/bin/qodercli/``), building a concrete local launch command. It
NEVER downloads or runs ``npx`` -- a candidate that does not resolve on disk is simply not discovered.

A resolved candidate is then probed with the same conformance harness ``doctor`` uses
(:func:`rutherford.acp.conformance.probe_agent`), because "found on disk" is not "drives over ACP" -- the
probe is the only trustworthy signal, exactly as it is for the built-in roster.

Trust model. Probing SPAWNS the resolved binary (with the registry-supplied args) before the ACP handshake,
so discovery executes installed programs that match a registry agent's name -- including ones found off PATH
under ``~/.<vendor>/bin``. Two guards bound this: an agent is never resolved to a shell/interpreter
(:func:`_is_interpreter`), so a tampered registry cannot name ``powershell``/``python`` with hostile args;
and the registry default is the official HTTPS endpoint (a ``file://`` or ``$RUTHERFORD_ACP_REGISTRY_URL``
override, and the on-disk cache, are trusted inputs the user controls). What remains -- a malicious binary
planted under a scanned dir whose name matches an agent -- requires write access to the user's home and is
no worse than the existing trust in PATH; ``discover`` is an explicit, user-invoked action, like ``doctor``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .conformance import probe_agent
from .descriptors import AgentDescriptor
from .registry import RegistryAgent

#: Executable extensions tried when scanning install dirs by bare name (PATH lookups use the OS PATHEXT
#: via :func:`shutil.which`). Covers the Windows launcher shims and the bare POSIX binary.
_EXE_SUFFIXES = ("", ".exe", ".cmd", ".bat")

#: Shell/interpreter FAMILY names a discovered agent must never resolve to: programs that execute their args
#: as code. The registry supplies the launch bin name AND args, and probing spawns ``(resolved_path, *args)``
#: before the ACP handshake -- so a hostile/poisoned registry that named e.g. ``powershell`` with arbitrary
#: args would otherwise get them run. :func:`_is_interpreter` classifies by the leading-letter FAMILY (after
#: stripping a script/exe extension and an optional ``w``/``m`` window-debug variant), so EVERY qualifier form
#: collapses to the family and is caught: versioned (``python3.12``), debug/preview (``python3-dbg``,
#: ``pwsh-preview``), variant (``pythonw``, ``pyw``), and shim (``node.cmd``, ``npx.cmd``). Matching the family
#: rather than enumerating forms ends the bypass arms race. No real ACP agent binary is an interpreter, so
#: this costs no legitimate agent (a real ``gemini.cmd`` shim has family ``gemini``; ``share-cli`` -> family
#: ``share``). Defense in depth: the registry default is the official HTTPS endpoint, and this guards a
#: tampered cache / URL override / MITM -- it is not claimed to be an exhaustive interpreter list.
_INTERPRETER_STEMS = frozenset(
    {
        # shells
        "powershell",
        "pwsh",
        "cmd",
        "command",
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "ash",
        "ksh",
        "csh",
        "tcsh",
        "busybox",
        "nu",
        "xonsh",
        "elvish",
        # general-purpose interpreters / runtimes
        "python",
        "py",
        "perl",
        "ruby",
        "php",
        "node",
        "nodejs",
        "deno",
        "bun",
        "lua",
        "luajit",
        "tclsh",
        "wish",
        "expect",
        "r",
        "rscript",
        "rterm",
        "rgui",
        "julia",
        "raku",
        "elixir",
        "erl",
        "escript",
        "crystal",
        "nim",
        "zig",
        "dart",
        "swift",
        "ghc",
        "runghc",
        "runhaskell",
        "babashka",
        "bb",
        "groovy",
        "scala",
        "kotlin",
        "clojure",
        "clj",
        "jshell",
        "java",
        # text-processing mini-languages that run code from args (letter-prefixed families only)
        "awk",
        "gawk",
        "sed",
        "jq",
        "bc",
        "dc",
        # package runners that fetch+execute
        "npx",
        "npm",
        "pnpm",
        "yarn",
        "bunx",
        "uvx",
        "uv",
        "pipx",
        # platform launchers / privilege / debuggers that take a program + args
        "env",
        "wsl",
        "sudo",
        "doas",
        "osascript",
        "cscript",
        "wscript",
        "rundll32",
        "regsvr32",
        "mshta",
        "dotnet",
        "gdb",
        "lldb",
    }
)
#: Executable / script extensions stripped before the family check, so a shim name (``npx.cmd``,
#: ``powershell.exe``) is matched on its stem. PATH lookups already honor PATHEXT, so this only affects the
#: guard, not resolution.
_EXE_EXT_RE = re.compile(r"\.(?:exe|cmd|bat|com|ps1|vbs|js|wsf|msc|scr)$", re.IGNORECASE)
#: The leading run of letters in a binary name: its interpreter FAMILY (``python3.12`` -> ``python``).
_LEADING_ALPHA_RE = re.compile(r"^[a-z]+")


def _is_interpreter(bin_name: str) -> bool:
    """Whether ``bin_name`` is a shell/interpreter that would execute registry-supplied args as code.

    Classifies by the leading-letter family, so a version (``python3.12``), debug/preview suffix
    (``python3-dbg``), shim extension (``node.cmd``), or ``w``/``m`` variant (``pythonw``, ``pyw``) all reduce
    to the family and are caught -- without false-positiving on a longer agent name (``share-cli`` -> family
    ``share``, ``nodecli`` -> ``nodecli``).
    """
    head = _LEADING_ALPHA_RE.match(_EXE_EXT_RE.sub("", bin_name.lower()))
    if head is None:
        return False
    family = head.group(0)
    # A window/debug variant attaches a trailing letter to the family (pythonw, pyw, rubyw, javaw).
    return family in _INTERPRETER_STEMS or (family[-1] in ("w", "m") and family[:-1] in _INTERPRETER_STEMS)


#: Registry ids that name an agent Rutherford already ships as a built-in under a DIFFERENT id, so discovery
#: recognizes it as already-in-roster and does not propose a duplicate seat. The registry's own ids
#: (``codex-acp``, ``mistral-vibe``, ...) differ from the built-in ids (``codex``, ``vibe``, ...); same agent.
REGISTRY_ID_ALIASES = {
    "claude-acp": "claude_code",
    "codex-acp": "codex",
    "factory-droid": "droid",
    "github-copilot-cli": "copilot",
    "github-copilot": "copilot",  # defensive: a future registry rename must not propose a duplicate copilot
    "mistral-vibe": "vibe",
    "pi-acp": "pi",
    "qwen-code": "qwen",
}


@dataclass(frozen=True, slots=True)
class DiscoveredAgent:
    """A registry agent found installed locally, with its resolved launch command and probe outcome."""

    id: str
    name: str
    description: str
    #: The concrete local launch argv: the resolved binary path followed by the distribution's args.
    command: tuple[str, ...]
    #: The resolved binary path on disk.
    found_at: str
    #: Whether an agent with this id is already in the live roster (a built-in / configured agent).
    already_in_roster: bool
    #: The probe result: ``ok`` | ``no_answer`` | ``handshake_failed`` | ``not_installed`` | ``error``, or
    #: ``None`` when probing was skipped.
    status: str | None
    detail: str


def resolve_local_command(
    agent: RegistryAgent,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[tuple[str, ...], str] | None:
    """Resolve ``agent``'s first candidate that exists on disk to ``(command, found_at)``, or ``None``.

    Tries each ``(bin_name, args)`` candidate in order: PATH first (:func:`shutil.which`), then the curated
    install dirs. The first hit wins and becomes ``(resolved_path, *args)``. Pure filesystem inspection -- no
    process is spawned and nothing is downloaded.
    """
    roots = _install_roots(env, home)
    for bin_name, args in agent.candidates:
        if _is_interpreter(bin_name):
            continue  # never resolve an agent to a shell/interpreter (registry-directed-execution guard)
        found = _which(bin_name, env) or _scan_roots(bin_name, roots)
        if found is not None:
            return (found, *args), found
    return None


async def discover_agents(
    agents: list[RegistryAgent],
    *,
    known_ids: set[str],
    probe: bool = True,
    probe_timeout_s: float = 45.0,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[DiscoveredAgent]:
    """Resolve every registry agent against the local filesystem and (optionally) probe the ones found.

    Returns one :class:`DiscoveredAgent` per agent resolved on disk, sorted by id. ``known_ids`` are the
    roster ids already present, so a discovered agent can be flagged as new vs already-configured. Probes
    run concurrently (like ``doctor``); ``probe=False`` returns the detection without spawning anything.
    """
    resolved: list[tuple[RegistryAgent, tuple[str, ...], str]] = []
    for agent in agents:
        match = resolve_local_command(agent, env=env, home=home)
        if match is not None:
            command, found_at = match
            resolved.append((agent, command, found_at))

    statuses: list[tuple[str | None, str]]
    if probe:
        reports = await asyncio.gather(
            *(
                probe_agent(
                    AgentDescriptor(id=agent.id, display_name=agent.name, command=command),
                    timeout_s=probe_timeout_s,
                )
                for agent, command, _found in resolved
            )
        )
        statuses = [(report.status, report.detail) for report in reports]
    else:
        statuses = [(None, "not probed") for _ in resolved]

    discovered = [
        DiscoveredAgent(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            command=command,
            found_at=found_at,
            already_in_roster=_in_roster(agent.id, known_ids),
            status=status,
            detail=detail,
        )
        for (agent, command, found_at), (status, detail) in zip(resolved, statuses, strict=True)
    ]
    return sorted(discovered, key=lambda item: item.id)


def _in_roster(registry_id: str, known_ids: set[str]) -> bool:
    """Whether ``registry_id`` (or its built-in alias) is already a roster agent -- so it is not re-proposed."""
    return registry_id in known_ids or REGISTRY_ID_ALIASES.get(registry_id, registry_id) in known_ids


def _install_roots(env: Mapping[str, str] | None, home: Path | None) -> list[Path]:
    """Curated dirs to scan beyond PATH: ``~/.local/bin``, ``~/.cargo/bin``, and every ``~/.<vendor>/bin``.

    The ``~/.<vendor>/bin`` glob is where many agent CLIs install off PATH (Qoder -> ``~/.qoder/bin``,
    several -> ``~/.<tool>/bin``); scanning it (bounded, one hidden-dir level) is what lets discovery find a
    custom-path install that ``shutil.which`` misses.
    """
    base = home if home is not None else _home(env)
    roots = [base / ".local" / "bin", base / ".cargo" / "bin"]
    with contextlib.suppress(OSError):  # an unreadable home is not fatal -- PATH detection still runs
        roots.extend(path for path in base.glob(".*/bin") if path.is_dir())
    return [root for root in roots if root.is_dir()]


def _scan_roots(bin_name: str, roots: list[Path]) -> str | None:
    """Find ``bin_name`` (with an executable suffix) directly in a root or one subdir deep; ``None`` if absent.

    One-subdir depth catches a nested install layout like ``~/.qoder/bin/qodercli/qodercli.exe`` while
    staying bounded. Returns the resolved absolute path as a string.
    """
    for root in roots:
        hit = _match_in_dir(bin_name, root)
        if hit is not None:
            return hit
        try:
            subdirs = [child for child in root.iterdir() if child.is_dir()]
        except OSError:
            continue
        for child in subdirs:
            hit = _match_in_dir(bin_name, child)
            if hit is not None:
                return hit
    return None


def _match_in_dir(bin_name: str, directory: Path) -> str | None:
    """Return the path of ``bin_name`` (trying each executable suffix) in ``directory``, or ``None``."""
    for suffix in _EXE_SUFFIXES:
        candidate = directory / f"{bin_name}{suffix}"
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _which(bin_name: str, env: Mapping[str, str] | None) -> str | None:
    """``shutil.which`` honoring an injected PATH (so a test can point detection at a fixture tree)."""
    path = None if env is None else env.get("PATH")
    return shutil.which(bin_name, path=path)


def _home(env: Mapping[str, str] | None) -> Path:
    """The home directory, honoring an injected environment (``USERPROFILE`` / ``HOME``) for tests."""
    environ = os.environ if env is None else env
    raw = environ.get("USERPROFILE") or environ.get("HOME")
    return Path(raw) if raw else Path.home()
