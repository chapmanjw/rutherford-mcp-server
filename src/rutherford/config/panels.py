# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Saved consensus/debate panels, loaded from ``panels.toon`` on disk.

A panel is a named, reusable set of targets -- the crew you keep reaching for -- so a caller can
say ``panel="design-roundtable"`` instead of spelling out the targets every time. Panels are
discovered across three locations and merged by name, closest scope winning: a project's
``<cwd>/.rutherford/panels.toon`` overrides your global ``~/.rutherford/panels.toon`` for a panel
of the same name, and an explicit ``$RUTHERFORD_CONFIG_DIR`` overrides both. This mirrors how the
TOML config treats a project ``rutherford.toml`` over the global ``config.toml``.

Files are TOON (the format the rest of Rutherford already speaks), read through the
:mod:`rutherford.io.serialize` decode seam. Loading is lazy and cached for the process via
:class:`PanelCache`; the ``reload_panels`` tool clears the cache. Validation runs over every
discovered file at load and reports every problem in one pass rather than failing on the first.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..domain.enums import Stance
from ..domain.error_codes import ErrorCode
from ..domain.errors import RutherfordError
from ..domain.models import Target
from ..io.serialize import DecodeError, decode
from .locations import config_scopes

#: The file searched in each config location.
PANELS_FILENAME = "panels.toon"

#: The keys a panel record and a panel target may carry. Anything else is a validation error.
_PANEL_KEYS = frozenset({"description", "strategy", "targets"})
_TARGET_KEYS = frozenset({"cli", "model", "role", "label", "weight", "parity", "stance"})


class PanelTarget(BaseModel):
    """One seat in a panel.

    ``cli`` is required; the rest default. ``role`` / ``label`` / ``weight`` / ``parity`` are carried
    so the panel file is forward-compatible, and become fully effective with the per-target metadata
    on :class:`~rutherford.domain.models.Target`. ``stance`` steers the seat for/against/neutral.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    cli: str
    model: str | None = None
    role: str | None = None
    label: str | None = None
    weight: float = 1.0
    parity: bool = False
    stance: Stance | None = None


class Panel(BaseModel):
    """A named panel: a description, an aggregation strategy, and the ordered targets."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    #: How a consensus over this panel is aggregated. ``all-voices`` returns every voice (today's
    #: behavior); the other strategies are wired in a later change. Stored now so the file is stable.
    strategy: str = "all-voices"
    targets: list[PanelTarget]

    def to_targets(self) -> list[Target]:
        """The panel's seats as delegation targets (cli + model; richer fields wire in later)."""
        return [Target(cli=target.cli, model=target.model) for target in self.targets]

    def stances(self) -> list[Stance] | None:
        """The per-seat stances, parallel to :meth:`to_targets`, or ``None`` if no seat sets one.

        When at least one seat is steered, every seat needs an explicit stance for the parallel
        list to line up, so an unsteered seat resolves to :attr:`Stance.NEUTRAL`.
        """
        if all(target.stance is None for target in self.targets):
            return None
        return [target.stance or Stance.NEUTRAL for target in self.targets]


class PanelStore:
    """An in-memory ``name -> Panel`` mapping."""

    def __init__(self, panels: dict[str, Panel]) -> None:
        self._panels = panels

    def get(self, name: str) -> Panel:
        """Return the panel named ``name`` or raise :class:`RutherfordError` listing what is available."""
        try:
            return self._panels[name]
        except KeyError:
            known = ", ".join(self.names()) or "(none defined)"
            raise RutherfordError(
                ErrorCode.PANEL_NOT_FOUND,
                f"unknown panel {name!r}; available panels: {known}",
                details={"panel": name, "available": self.names()},
            ) from None

    def has(self, name: str) -> bool:
        """Return whether a panel named ``name`` is loaded."""
        return name in self._panels

    def names(self) -> list[str]:
        """Return the loaded panel names, sorted."""
        return sorted(self._panels)

    def all(self) -> list[Panel]:
        """Return every loaded panel, ordered by name."""
        return [self._panels[name] for name in self.names()]


class PanelCache:
    """Lazily loads a :class:`PanelStore` once and caches it for the process; ``reload`` refreshes it."""

    def __init__(self, loader: Callable[[], PanelStore]) -> None:
        self._loader = loader
        self._store: PanelStore | None = None

    @classmethod
    def seeded(cls, store: PanelStore) -> PanelCache:
        """A cache pre-populated with ``store`` (for tests and injection); ``reload`` re-runs nothing."""
        cache = cls(lambda: store)
        cache._store = store
        return cache

    def store(self) -> PanelStore:
        """Return the cached store, loading it on first use."""
        if self._store is None:
            self._store = self._loader()
        return self._store

    def reload(self) -> PanelStore:
        """Re-read every panels file from disk and replace the cached store."""
        self._store = self._loader()
        return self._store

    def names(self) -> list[str]:
        """The names of the loaded panels (loading on first use)."""
        return self.store().names()

    def get(self, name: str) -> Panel:
        """Look up a panel by name (loading on first use)."""
        return self.store().get(name)

    def resolve(self, name: str, overrides: Mapping[str, Any] | None = None) -> Panel:
        """Look up a panel and apply optional one-off ``overrides`` (a shallow merge over its record)."""
        panel = self.store().get(name)
        if not overrides:
            return panel
        record = panel.model_dump(mode="json")
        record.update(overrides)
        try:
            return Panel.model_validate(record)
        except Exception as exc:  # a bad override should read as invalid input, not crash the tool
            raise RutherfordError(
                ErrorCode.PANEL_INVALID,
                f"panel_overrides produced an invalid panel {name!r}: {exc}",
                details={"panel": name, "overrides": dict(overrides)},
            ) from exc


def load_panels(
    known_clis: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> PanelStore:
    """Discover, merge, and validate every ``panels.toon``; raise once with all problems if any fail.

    Locations are read lowest to highest precedence (user, then project, then ``$RUTHERFORD_CONFIG_DIR``)
    and panels merge by name, so a closer scope overrides a farther one. ``known_clis`` is the set of
    registered adapter ids; a panel target naming an unknown CLI is a validation error pointing at the
    offending file and target index.

    Raises:
        RutherfordError: ``PANEL_INVALID`` if any discovered file fails to parse or validate.
    """
    environ = os.environ if env is None else env
    working_dir = Path.cwd() if cwd is None else Path(cwd)
    valid_clis = set(known_clis)

    merged: dict[str, Panel] = {}
    problems: list[dict[str, Any]] = []

    for _source, directory in config_scopes(environ, working_dir):
        path = directory / PANELS_FILENAME
        if not path.exists():
            continue
        try:
            raw = decode(path.read_text(encoding="utf-8"))
        except (DecodeError, OSError) as exc:
            problems.append({"path": str(path), "error": f"could not read panels file: {exc}"})
            continue
        section = raw.get("panels") if isinstance(raw, dict) else None
        if not isinstance(section, dict):
            problems.append({"path": str(path), "error": "expected a top-level 'panels' table"})
            continue
        for name, record in section.items():
            panel, panel_problems = _parse_panel(str(name), record, valid_clis, str(path))
            problems.extend(panel_problems)
            if panel is not None:
                merged[name] = panel  # a closer location overrides a farther one for the same name

    if problems:
        raise RutherfordError(ErrorCode.PANEL_INVALID, _summarize(problems), details={"problems": problems})
    return PanelStore(merged)


def _parse_panel(
    name: str,
    record: Any,
    valid_clis: set[str],
    path: str,
) -> tuple[Panel | None, list[dict[str, Any]]]:
    """Validate one panel record, collecting every problem; return the built panel or ``None``."""
    problems: list[dict[str, Any]] = []
    if not isinstance(record, dict):
        return None, [{"path": path, "panel": name, "error": "panel must be a table"}]

    for key in record:
        if key not in _PANEL_KEYS:
            problems.append({"path": path, "panel": name, "error": f"unknown panel key {key!r}"})

    raw_targets = record.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        problems.append({"path": path, "panel": name, "error": "panel needs a non-empty 'targets' list"})
        raw_targets = []

    clean_targets: list[dict[str, Any]] = []
    for index, raw_target in enumerate(raw_targets):
        cleaned, target_problems = _parse_target(name, index, raw_target, valid_clis, path)
        problems.extend(target_problems)
        if cleaned is not None:
            clean_targets.append(cleaned)

    if problems:
        return None, problems

    panel = Panel(
        name=name,
        description=str(record.get("description", "")),
        strategy=str(record.get("strategy", "all-voices")),
        targets=[PanelTarget(**target) for target in clean_targets],
    )
    return panel, []


def _parse_target(
    panel: str,
    index: int,
    raw: Any,
    valid_clis: set[str],
    path: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Validate one target record, collecting every problem; return cleaned kwargs or ``None``."""
    here = {"path": path, "panel": panel, "target": index}
    if not isinstance(raw, dict):
        return None, [{**here, "error": "target must be a table"}]

    problems: list[dict[str, Any]] = []
    for key in raw:
        if key not in _TARGET_KEYS:
            problems.append({**here, "error": f"unknown target key {key!r}"})

    cli = raw.get("cli")
    if not isinstance(cli, str) or not cli:
        problems.append({**here, "error": "target needs a non-empty 'cli'"})
    elif cli not in valid_clis:
        known = ", ".join(sorted(valid_clis)) or "(none)"
        problems.append({**here, "error": f"unknown cli {cli!r}; known adapters: {known}"})

    stance = raw.get("stance")
    if stance is not None and stance not in {member.value for member in Stance}:
        options = ", ".join(member.value for member in Stance)
        problems.append({**here, "error": f"unknown stance {stance!r}; choose one of: {options}"})

    weight = raw.get("weight")
    if weight is not None and not isinstance(weight, (int, float)):
        problems.append({**here, "error": "weight must be a number"})

    parity = raw.get("parity")
    if parity is not None and not isinstance(parity, bool):
        problems.append({**here, "error": "parity must be true or false"})

    if problems:
        return None, problems
    return {key: raw[key] for key in _TARGET_KEYS if key in raw}, []


def _summarize(problems: list[dict[str, Any]]) -> str:
    """Render every collected problem into one multi-line message."""
    lines = [f"{len(problems)} problem(s) in panels files:"]
    for problem in problems:
        location = problem["path"]
        if "panel" in problem:
            location += f" [panel {problem['panel']}]"
        if "target" in problem:
            location += f" [target {problem['target']}]"
        lines.append(f"  - {location}: {problem['error']}")
    return "\n".join(lines)
