# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for the ACP registry client: parsing the distribution schema, fetch, and cache fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rutherford.acp.registry import (
    RegistryError,
    _bin_basename,
    _npx_bin_names,
    _platform_key,
    fetch_registry,
    parse_registry,
)

# A miniature registry covering both distribution forms plus a malformed/empty entry to skip.
_FIXTURE = {
    "version": "1.0.0",
    "agents": [
        {
            "id": "gemini",
            "name": "Gemini CLI",
            "description": "Google's CLI",
            "distribution": {"npx": {"package": "@google/gemini-cli@0.46.0", "args": ["--acp"]}},
        },
        {
            "id": "goose",
            "name": "goose",
            "distribution": {
                "binary": {
                    "windows-x86_64": {
                        "archive": "https://x/y.zip",
                        "cmd": "./goose-package\\goose.exe",
                        "args": ["acp"],
                    },
                    "linux-x86_64": {"archive": "https://x/y.tar", "cmd": "./goose", "args": ["acp"]},
                    "darwin-aarch64": {"archive": "https://x/y.tar", "cmd": "./goose", "args": ["acp"]},
                }
            },
        },
        {"id": "download-only", "name": "No Local Form", "distribution": {}},  # no npx/binary -> skipped
        {"name": "no-id-and-no-dist"},  # skipped
    ],
}


def _write(tmp_path: Path, data: object) -> str:
    target = tmp_path / "registry.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target.as_uri()


def test_parse_extracts_candidates_for_both_forms() -> None:
    agents = parse_registry(json.dumps(_FIXTURE).encode("utf-8"))
    by_id = {a.id: a for a in agents}
    assert set(by_id) == {"gemini", "goose"}  # the two unlaunchable entries are dropped
    # npx -> the bare package name plus a suffix-stripped guess, both paired with the npx args.
    assert by_id["gemini"].candidates[0] == ("gemini-cli", ("--acp",))
    assert ("gemini", ("--acp",)) in by_id["gemini"].candidates
    assert by_id["gemini"].bin_names == ("gemini-cli", "gemini")
    # binary -> the basename of the current platform's cmd (or any platform), with the binary's args.
    goose_names = by_id["goose"].bin_names
    assert goose_names == ("goose",) and by_id["goose"].candidates[0][1] == ("acp",)


def test_npx_bin_names_strips_a_scope_and_one_suffix() -> None:
    assert _npx_bin_names("@google/gemini-cli@0.46.0") == ["gemini-cli", "gemini"]
    assert _npx_bin_names("@qoder-ai/qodercli@1.0.0") == ["qodercli"]  # no known suffix to strip
    assert _npx_bin_names("pi-acp@0.0.28") == ["pi-acp", "pi"]


def test_bin_basename_handles_windows_and_posix_paths() -> None:
    assert _bin_basename("./goose-package\\goose.exe") == "goose"
    assert _bin_basename("./goose") == "goose"
    assert _bin_basename("bin/grok.EXE") == "grok"


def test_platform_key_is_os_arch() -> None:
    key = _platform_key()
    assert key.split("-")[0] in {"windows", "darwin", "linux"}


def test_fetch_from_a_file_url_and_cache(tmp_path: Path) -> None:
    url = _write(tmp_path, _FIXTURE)
    cache = tmp_path / "cache" / "acp-registry.json"
    agents, source = fetch_registry(url=url, cache_path=cache)
    assert source == "network" and {a.id for a in agents} == {"gemini", "goose"}
    assert cache.exists()  # a successful fetch refreshes the cache


def test_falls_back_to_cache_when_the_network_fails(tmp_path: Path) -> None:
    cache = tmp_path / "acp-registry.json"
    cache.write_text(json.dumps(_FIXTURE), encoding="utf-8")
    # A bogus URL that cannot be opened forces the cache fallback.
    agents, source = fetch_registry(url=(tmp_path / "missing.json").as_uri(), cache_path=cache)
    assert source == "cache" and {a.id for a in agents} == {"gemini", "goose"}


def test_raises_when_neither_network_nor_cache(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        fetch_registry(url=(tmp_path / "missing.json").as_uri(), cache_path=tmp_path / "no-cache.json")


def test_parse_rejects_non_json_and_missing_agents() -> None:
    with pytest.raises(RegistryError):
        parse_registry(b"not json{{")
    with pytest.raises(RegistryError):
        parse_registry(json.dumps({"version": "1.0.0"}).encode("utf-8"))
