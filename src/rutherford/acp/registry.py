# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ACP agent registry: fetch, cache, and parse the community registry of ACP-capable agents.

The Agent Client Protocol project publishes a registry of agents that speak ACP -- their launch
distribution (an npm package run via ``npx``, or a per-platform downloadable binary), not a way to
detect one already installed. This module fetches that JSON, caches it locally, and parses each entry
into a :class:`RegistryAgent` that exposes the *candidate local launch commands* a detector can look
for on disk. It never downloads or runs an agent -- it only turns the registry into something
:mod:`rutherford.acp.discovery` can match against what is already installed.

Fetching is bounded and degrades gracefully: a network failure falls back to the on-disk cache, so
discovery still works offline once the registry has been fetched once. The CDN rejects the default
``urllib`` User-Agent (HTTP 403), so a real UA header is always sent.
"""

from __future__ import annotations

import json
import logging
import platform
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request

_log = logging.getLogger(__name__)

#: The canonical published registry (latest snapshot). Overridable via ``RUTHERFORD_ACP_REGISTRY_URL``
#: (e.g. a ``file://`` fixture in tests, or a pinned snapshot).
DEFAULT_REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
#: The cache filename under ``~/.rutherford/``; a successful fetch is written here for offline reuse.
CACHE_FILENAME = "acp-registry.json"
#: A real User-Agent: the registry CDN returns 403 to the default ``Python-urllib/x.y`` agent.
_USER_AGENT = "rutherford-acp/3 (+https://github.com/chapmanjw)"
#: Suffixes stripped from an npm package's bare name to guess the installed bin name
#: (``@google/gemini-cli`` -> bin ``gemini``; ``@qoder-ai/qodercli`` keeps ``qodercli``).
_BIN_SUFFIXES = ("-cli", "-code", "-acp", "-agent")


@dataclass(frozen=True, slots=True)
class RegistryAgent:
    """One registry entry, reduced to what local discovery needs.

    ``candidates`` is the list of ``(bin_name, args)`` a detector should look for on PATH / known install
    dirs -- derived from the platform binary's command and from the npm package's likely bin name. Pairing
    each name with its own args matters: a binary distribution launches with the binary's args (``goose
    acp``), an npm-installed one with the npx args (``gemini --acp``).
    """

    id: str
    name: str
    description: str
    #: ``(bin_name, args)`` launch candidates to resolve against the filesystem, best first.
    candidates: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def bin_names(self) -> tuple[str, ...]:
        """The distinct candidate binary names (order-preserving)."""
        seen: dict[str, None] = {}
        for name, _args in self.candidates:
            seen.setdefault(name, None)
        return tuple(seen)


def fetch_registry(
    *,
    url: str = DEFAULT_REGISTRY_URL,
    cache_path: Path | None = None,
    timeout_s: float = 15.0,
    force_refresh: bool = False,
) -> tuple[list[RegistryAgent], str]:
    """Return the parsed registry agents and a ``source`` tag (``network`` | ``cache``).

    Tries the network first (unless the only need is the cache); on any network failure falls back to the
    on-disk ``cache_path`` if present. A successful network fetch refreshes the cache. ``force_refresh`` is
    accepted for symmetry but the network is always tried first anyway. Raises :class:`RegistryError` only
    when BOTH the network and the cache are unavailable, so a caller can surface a clean message.
    """
    raw: bytes | None = None
    source = "network"
    try:
        raw = _fetch_url(url, timeout_s)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("ACP registry fetch failed (%s); falling back to cache", exc)
    if raw is not None and cache_path is not None:
        _write_cache(cache_path, raw)
    if raw is None and cache_path is not None and cache_path.exists():
        raw = cache_path.read_bytes()
        source = "cache"
    if raw is None:
        raise RegistryError(
            f"could not fetch the ACP registry from {url} and no cache is available; check the network "
            "or set RUTHERFORD_ACP_REGISTRY_URL"
        )
    _ = force_refresh  # network is always tried first; the flag is a no-op kept for a stable signature
    return parse_registry(raw), source


class RegistryError(Exception):
    """The ACP registry could not be fetched or parsed."""


def parse_registry(raw: bytes) -> list[RegistryAgent]:
    """Parse the registry JSON bytes into :class:`RegistryAgent` records (malformed entries are skipped)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RegistryError(f"the ACP registry is not valid JSON: {exc}") from exc
    entries = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise RegistryError("the ACP registry has no 'agents' list")
    agents: list[RegistryAgent] = []
    for entry in entries:
        agent = _parse_agent(entry)
        if agent is not None:
            agents.append(agent)
    return agents


def _parse_agent(entry: object) -> RegistryAgent | None:
    """Reduce one raw registry entry to a :class:`RegistryAgent`, or ``None`` if it cannot launch locally."""
    if not isinstance(entry, dict):
        return None
    agent_id = entry.get("id") or entry.get("name")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    distribution = entry.get("distribution")
    candidates: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(distribution, dict):
        candidates.extend(_binary_candidates(distribution.get("binary")))
        candidates.extend(_npx_candidates(distribution.get("npx")))
    if not candidates:
        return None  # no usable local launch form (e.g. download-only with no current-platform binary)
    name_value = entry.get("name")
    name = name_value if isinstance(name_value, str) else agent_id
    description_value = entry.get("description")
    description = description_value if isinstance(description_value, str) else ""
    return RegistryAgent(id=agent_id, name=name, description=description, candidates=tuple(candidates))


def _binary_candidates(binary: object) -> list[tuple[str, tuple[str, ...]]]:
    """The ``(bin_name, args)`` candidate from the binary distribution for the current platform, if any."""
    if not isinstance(binary, dict):
        return []
    spec = binary.get(_platform_key()) or _any_platform(binary)
    if not isinstance(spec, dict):
        return []
    cmd = spec.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        return []
    args = _str_tuple(spec.get("args"))
    return [(_bin_basename(cmd), args)]


def _npx_candidates(npx: object) -> list[tuple[str, tuple[str, ...]]]:
    """The ``(bin_name, args)`` candidates from the npx distribution (the likely installed-bin names)."""
    if not isinstance(npx, dict):
        return []
    package = npx.get("package")
    if not isinstance(package, str) or not package:
        return []
    args = _str_tuple(npx.get("args"))
    return [(name, args) for name in _npx_bin_names(package)]


def _npx_bin_names(package: str) -> list[str]:
    """Likely installed bin names for an npm package: the bare name, plus a common-suffix-stripped form.

    ``@google/gemini-cli@0.46.0`` -> ``["gemini-cli", "gemini"]``; ``@qoder-ai/qodercli@1`` -> ``["qodercli"]``;
    ``pi-acp@0.0.28`` -> ``["pi-acp", "pi"]``. Heuristic on purpose -- a name we cannot resolve on disk is
    simply not detected (never downloaded), so a wrong guess costs nothing.
    """
    bare = package.split("@")[-2] if package.startswith("@") else package.split("@")[0]
    bare = bare.rsplit("/", 1)[-1]  # drop the npm scope
    names = [bare]
    for suffix in _BIN_SUFFIXES:
        if bare.endswith(suffix) and len(bare) > len(suffix):
            names.append(bare[: -len(suffix)])
            break
    return names


def _bin_basename(cmd: str) -> str:
    """The bare binary name from a registry ``cmd`` path (``./goose-package\\goose.exe`` -> ``goose``)."""
    tail = cmd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.lower().endswith(".exe") else tail


def _platform_key() -> str:
    """The registry's platform key for the current machine (e.g. ``windows-x86_64``, ``darwin-aarch64``)."""
    system = platform.system().lower()
    os_name = {"windows": "windows", "darwin": "darwin", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine, machine)
    return f"{os_name}-{arch}"


def _any_platform(binary: dict[str, object]) -> object:
    """Any platform spec, so a binary agent still yields a candidate bin name on an unlisted platform."""
    return next(iter(binary.values()), None)


def _str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a JSON args array to a tuple of strings (non-strings dropped)."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _fetch_url(url: str, timeout_s: float) -> bytes:
    """GET ``url`` with a real User-Agent and return the body bytes (raises on failure)."""
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        body: bytes = response.read()
    return body


def _write_cache(cache_path: Path, raw: bytes) -> None:
    """Best-effort cache write: a failure is logged, never raised (the fetch already succeeded)."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    except OSError as exc:
        _log.debug("could not cache the ACP registry at %s: %s", cache_path, exc)
