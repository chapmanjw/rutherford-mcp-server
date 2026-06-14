# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Reasoning-effort tiers mapped to a per-call ACP override (F8a, decision 2-L).

ACP has no protocol field for reasoning effort, so a tier becomes per-agent launch args, env, or a model-id
rewrite -- the only places an effort knob can live over ACP. :func:`effort_overrides` is the one mapping
table: it returns the extra args / env / rewritten model an :class:`~rutherford.acp.session.ACPSession`
layers onto the descriptor for a single turn, plus the ``applied`` tier (clamped to what the agent supports)
and a human ``note`` for the result. An agent with no effort knob is a reported no-op (``applied=None``), not
a silent drop.

Only the agents whose knob is verifiable are wired, and each through its real ACP mechanism -- so a budget
that did nothing is never reported as if it did:

* ``codex`` (``codex-acp``) -- effort rides the ACP **model id** as ``model[effort]`` (bracket syntax the
  adapter parses; vocabulary includes ``xhigh``). Needs a concrete model to rewrite; a no-op when none.
* ``cursor`` (``cursor-agent``) -- effort rides the model id as a ``model-<tier>`` suffix (clamps ``xhigh``
  to ``high``). Needs a concrete model to rewrite; a no-op when none.
* ``cline`` (``cline --acp``) -- the global ``--thinking <tier>`` launch flag, valid alongside ``--acp`` and
  honored for every tier including ``xhigh``.
* ``junie`` (``junie --acp=true``) -- the ``JUNIE_EFFORT`` env var sets the default effort for new sessions;
  best-effort because the docs do not explicitly confirm it applies in ACP mode (see the note).

Every other agent (including ``pi``, whose ``--thinking`` is an in-session RPC selector with no launch knob)
is an honest no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..domain.enums import EFFORT_ORDER, Effort
from .descriptors import AgentDescriptor

#: Codex's reasoning-effort vocabulary tops out at ``xhigh`` (richer than the others), so no clamp applies.
_CODEX_CEILING = Effort.XHIGH


@dataclass(frozen=True, slots=True)
class EffortOverride:
    """How one effort tier is applied to one agent for a single ACP turn (F8a, 2-L-map).

    Mirrors the v2 ``EffortFlags`` but adds ``model``: codex and cursor encode effort in the model id, which
    Rutherford already hands to the ACP session, so the override channel for them is a rewritten model rather
    than an arg. ``extra_args`` are appended to the launch argv, ``extra_env`` is overlaid on the agent's
    environment, and ``model`` (when set) replaces the call's model for this turn. ``applied`` is the tier the
    agent will actually use after clamping, or ``None`` for an agent with no knob (a no-op). ``note`` records
    the mapping -- including a no-op or a clamp -- for the result, so a budget that did nothing is never silent.
    """

    extra_args: tuple[str, ...] = ()
    extra_env: tuple[tuple[str, str], ...] = ()
    model: str | None = None
    applied: Effort | None = None
    note: str = ""

    @property
    def env_dict(self) -> dict[str, str]:
        """The effort env as a plain dict, for layering onto the agent environment."""
        return dict(self.extra_env)


_NONE = EffortOverride()


def _clamp(effort: Effort, ceiling: Effort) -> Effort:
    """Clamp ``effort`` down to ``ceiling`` when the agent tops out below the requested tier (2-L-map)."""
    if EFFORT_ORDER.index(effort) <= EFFORT_ORDER.index(ceiling):
        return effort
    return ceiling


def effort_overrides(descriptor: AgentDescriptor, effort: Effort | None, *, model: str | None) -> EffortOverride:
    """Map ``effort`` to ``descriptor``'s per-call ACP override, or a reported no-op.

    ``model`` is the model already RESOLVED for this call (the call's model, else the descriptor default), so
    a model-id-encoding agent (codex, cursor) rewrites the model the session will actually use, not just the
    descriptor default. ``effort=None`` (no tier requested) is always a clean no-op. Otherwise the agent's own
    knob is consulted: a known agent gets its real mechanism (a model-id rewrite for codex/cursor, a launch
    flag for cline, an env var for junie), and an agent with no verifiable knob -- including ``pi`` -- gets a
    no-op carrying a note that says so. The dispatch keys on the agent ``id`` so a config clone keeps its
    base's knob only if it keeps the id.
    """
    if effort is None:
        return _NONE
    builder = _BUILDERS.get(descriptor.id)
    if builder is None:
        return EffortOverride(note=f"effort '{effort.value}' is not supported by {descriptor.id}; ignored")
    return builder(model, effort)


def _codex(model: str | None, effort: Effort) -> EffortOverride:
    """Codex: encode effort in the ACP model id as ``model[effort]`` (the ``codex-acp`` adapter parses it).

    The adapter advertises ``base[effort]`` model ids and validates the effort against the model, so the tier
    must ride a concrete model. With no model resolved there is nothing to rewrite, so this reports a no-op
    rather than guessing a base model.
    """
    applied = _clamp(effort, _CODEX_CEILING)
    if not model:
        return EffortOverride(
            note=f"effort '{effort.value}' needs a model for codex's 'model[effort]' id; none resolved, ignored"
        )
    note = f"reasoning effort via the codex model id '[{applied.value}]'"
    if applied is not effort:  # unreachable today (xhigh is the ceiling) but kept honest if the ceiling drops
        note += f" (clamped from {effort.value})"  # pragma: no cover
    return EffortOverride(model=_codex_model(model, applied), applied=applied, note=note)


def _cursor(model: str | None, effort: Effort) -> EffortOverride:
    """Cursor: encode effort in the model id as a ``-<tier>`` suffix (``gpt-5.2-high``); tops out at ``high``.

    Needs a concrete model to rewrite -- the suffix is part of the id, not a free-standing flag -- so a call
    with no model resolved is a reported no-op. An ``auto`` model, a model already carrying a tier, or a
    ``-thinking`` / ``-fast`` variant is left unchanged (the user's explicit choice wins).
    """
    applied = _clamp(effort, Effort.HIGH)
    if not model:
        return EffortOverride(
            note=f"effort '{effort.value}' needs a model for cursor's '-{applied.value}' suffix; none resolved, ignored"
        )
    rewritten = _cursor_model(model, applied)
    note = f"reasoning effort via the cursor model-id '-{applied.value}' suffix"
    if applied is not effort:
        note += f" (clamped from {effort.value})"
    if rewritten == model:
        note = f"cursor model {model!r} already carries an explicit reasoning tier; left unchanged"
        return EffortOverride(applied=applied, note=note)
    return EffortOverride(model=rewritten, applied=applied, note=note)


def _cline(model: str | None, effort: Effort) -> EffortOverride:
    """Cline: the global ``--thinking <tier>`` launch flag, valid with ``--acp`` and honoring every tier."""
    return EffortOverride(extra_args=("--thinking", effort.value), applied=effort, note=f"--thinking {effort.value}")


def _junie(model: str | None, effort: Effort) -> EffortOverride:
    """Junie: the ``JUNIE_EFFORT`` env var, the documented default effort for new sessions (best-effort).

    Junie's docs frame ``JUNIE_EFFORT`` (and the ``--effort`` flag) as the default for new sessions but do
    not explicitly confirm it applies in ACP mode; the env var is the safer of the two, so it is what is set,
    with the caveat recorded in the note. Junie supports every tier (its own clamp to a model's range is the
    agent's responsibility).
    """
    return EffortOverride(
        extra_env=(("JUNIE_EFFORT", effort.value),),
        applied=effort,
        note=f"JUNIE_EFFORT={effort.value} (best-effort: a new-session default; ACP-mode application unconfirmed)",
    )


def _codex_model(model: str, effort: Effort) -> str:
    """Append codex's ``[effort]`` bracket to a model id, replacing any bracket the model already carries."""
    base = model.split("[", 1)[0]
    return f"{base}[{effort.value}]"


def _cursor_model(model: str, effort: Effort) -> str:
    """Append cursor's ``-<tier>`` reasoning suffix to a bare model id, or leave it unchanged.

    Mirrors v2's guards: ``auto``, a ``-thinking`` extended-thinking variant, a ``-fast`` latency variant,
    and a model already ending in a reasoning tier are all returned untouched -- a plain ``-<tier>`` is the
    cross-family effort suffix, and an explicit tier the user chose is respected.
    """
    lowered = model.lower()
    if model == "auto" or "thinking" in lowered or lowered.endswith("-fast"):
        return model
    if any(lowered.endswith(f"-{tier.value}") for tier in EFFORT_ORDER):
        return model
    return f"{model}-{effort.value}"


#: One agent's effort builder: ``(resolved model, tier)`` -> its override. The ``None`` effort case
#: short-circuits before dispatch, so a builder always receives a concrete tier.
_Builder = Callable[[str | None, Effort], EffortOverride]

#: The per-agent effort builders, keyed by agent id. An id absent here has no effort knob (a reported no-op).
_BUILDERS: dict[str, _Builder] = {
    "codex": _codex,
    "cursor": _cursor,
    "cline": _cline,
    "junie": _junie,
}
