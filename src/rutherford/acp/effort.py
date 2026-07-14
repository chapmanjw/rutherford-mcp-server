# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Reasoning-effort tiers mapped to a per-call ACP override (F8a, decision 2-L).

ACP delivers a reasoning-effort tier one of four ways, and each wired agent uses its own real, verifiable
knob -- so a budget that did nothing is never reported as if it did. :func:`effort_overrides` is the one
mapping table for the *launch-time* channels (args / env / a model-id rewrite); the *session config-option*
channel is resolved later, at session open, by :class:`~rutherford.acp.session.ACPSession` from what the
agent actually advertises (the ``via_config_option`` flag below routes an agent there). An agent with no
effort knob is a reported no-op (``applied=None``), not a silent drop.

Launch-time channels (this table):

* ``cline`` (``cline --acp``) -- the global ``--thinking <tier>`` launch flag, every tier.
* ``kiro`` (``kiro-cli acp``) -- the ``--effort <tier>`` launch flag, vocabulary ``low..xhigh`` plus ``max``
  (so no clamp: it covers every Rutherford tier).
* ``junie`` (``junie --acp=true``) -- the ``JUNIE_EFFORT`` env var (best-effort; ACP-mode application
  unconfirmed -- see the note).
* ``cursor`` (``cursor-agent``) -- effort rides the model id: a ``model-<tier>`` suffix on bare ids, or an
  in-bracket ``effort=...`` update on compound Cursor ids like ``grok-4.5[effort=high,fast=true]`` (clamps to
  ``high``). Needs a concrete model; a no-op when none. The rewritten id is passed on the launch
  ``--model`` flag (:attr:`~rutherford.acp.descriptors.AgentDescriptor.model_launch_flag`), not via
  in-session ``set_model`` / ``set_config_option``.
* ``codex`` (``codex-acp``) WITH a concrete model -- effort rides the ACP **model id** as ``model[effort]``
  (the bracket syntax codex-acp advertises and parses; tops out at ``xhigh``), so one ``set_model`` selects
  both the model and the tier.

Session config-option channel (``via_config_option=True``; applied at open, not here):

* ``claude_code`` (``claude-agent-acp``) -- the ACP ``effort`` config option (values ``low..max``).
* ``codex`` WITHOUT a model -- the ACP ``reasoning_effort`` config option (values ``low..xhigh``), so a
  codex seat that pins no model still gets its tier on codex's default model.

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
    #: Route this agent's effort through the ACP **config-option** channel resolved at session open
    #: (claude_code's ``effort`` option, codex's ``reasoning_effort`` option) rather than a launch-time
    #: arg/env/model. When ``True``, the launch-time fields are empty and ``applied`` is ``None`` here --
    #: the session discovers the agent's advertised effort option, clamps the requested tier to its values,
    #: sets it, and reports the actually-applied tier. A no-op (``applied`` stays ``None``) if the agent
    #: advertises no such option after all.
    via_config_option: bool = False

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


def supports_effort(descriptor: AgentDescriptor) -> bool:
    """Whether Rutherford knows a reasoning-effort knob for ``descriptor`` (including clones via ``effort_base``).

    Static roster signal for ``capabilities``: true when :func:`effort_overrides` would apply a real mechanism
    for a concrete tier, false when any requested effort is a reported no-op.
    """
    return (descriptor.effort_base or descriptor.id) in _BUILDERS


def effort_overrides(descriptor: AgentDescriptor, effort: Effort | None, *, model: str | None) -> EffortOverride:
    """Map ``effort`` to ``descriptor``'s per-call ACP override, or a reported no-op.

    ``model`` is the model already RESOLVED for this call (the call's model, else the descriptor default), so
    a model-id-encoding agent (codex, cursor) rewrites the model the session will actually use, not just the
    descriptor default. ``effort=None`` (no tier requested) is always a clean no-op. Otherwise the agent's own
    knob is consulted: a known agent gets its real mechanism (a model-id rewrite for codex/cursor, a launch
    flag for cline, an env var for junie), and an agent with no verifiable knob -- including ``pi`` -- gets a
    no-op carrying a note that says so. The dispatch resolves on ``descriptor.effort_base or descriptor.id``:
    a built-in (``effort_base is None``) resolves by its own id, while a config clone of an effort-capable
    built-in carries its base id in ``effort_base`` (stamped by :func:`rutherford.acp.roster._merge`) and so
    inherits the base adapter's knob -- effort follows the launched adapter, not the new agent id. The no-op
    note still names ``descriptor.id`` (the seat the caller addressed).
    """
    if effort is None:
        return _NONE
    builder = _BUILDERS.get(descriptor.effort_base or descriptor.id)
    if builder is None:
        return EffortOverride(note=f"effort '{effort.value}' is not supported by {descriptor.id}; ignored")
    return builder(model, effort)


def _codex(model: str | None, effort: Effort) -> EffortOverride:
    """Codex: encode effort in the ACP model id as ``model[effort]`` when a model is pinned, else the
    ``reasoning_effort`` config option.

    codex-acp advertises ``base[effort]`` model ids (live-confirmed: ``gpt-5.5[low|medium|high|xhigh]``), so
    with a concrete model one ``set_model`` selects both the model and the tier. With NO model the bare id is
    not advertised (so ``set_model`` would be skipped); codex-acp also exposes a ``reasoning_effort`` config
    option, so this routes the no-model case there instead of dropping the tier (the old behavior). Codex tops
    out at ``xhigh``, so ``max`` clamps down -- reported on the model-id note; the config-option path clamps to
    the option's advertised values at open.
    """
    if not model:
        return EffortOverride(
            via_config_option=True,
            note=f"reasoning effort '{effort.value}' via codex's 'reasoning_effort' config option (no model pinned)",
        )
    applied = _clamp(effort, _CODEX_CEILING)
    note = f"reasoning effort via the codex model id '[{applied.value}]'"
    if applied is not effort:
        note += f" (clamped from {effort.value})"
    return EffortOverride(model=_codex_model(model, applied), applied=applied, note=note)


def _claude_code(model: str | None, effort: Effort) -> EffortOverride:
    """Claude Code: the ACP ``effort`` config option (resolved at session open).

    claude-agent-acp advertises an ``effort`` config option whose values are the current model's supported
    levels (live-confirmed ``default/low/medium/high/xhigh/max``), settable over ACP via ``set_config_option``
    -- not a launch flag and not the model id. So this just routes to the config-option channel; the session
    discovers the option, clamps the requested tier to the model's advertised levels, and applies it.
    """
    return EffortOverride(
        via_config_option=True,
        note=f"reasoning effort '{effort.value}' via claude_code's 'effort' config option",
    )


def _kiro(model: str | None, effort: Effort) -> EffortOverride:
    """Kiro: the ``kiro-cli acp --effort <tier>`` launch flag (live-confirmed ``low..xhigh`` plus ``max``).

    Kiro's vocabulary covers every Rutherford tier, so no clamp applies -- the requested tier rides the launch
    argv directly, like cline's ``--thinking``.
    """
    return EffortOverride(extra_args=("--effort", effort.value), applied=effort, note=f"--effort {effort.value}")


def _cursor(model: str | None, effort: Effort) -> EffortOverride:
    """Cursor: encode effort in the model id (suffix or in-bracket ``effort=``); tops out at ``high``.

    Needs a concrete model to rewrite -- effort is part of the id, not a free-standing flag -- so a call with
    no model resolved is a reported no-op. An ``auto`` model, a model already carrying the requested tier
    (suffix or ``effort=``), or a ``-thinking`` / bare ``-fast`` variant is left unchanged. The rewritten id
    is selected at spawn via Cursor's launch ``--model`` flag, not an in-session ACP model RPC.
    """
    applied = _clamp(effort, Effort.HIGH)
    if not model:
        return EffortOverride(
            note=f"effort '{effort.value}' needs a model for cursor's effort encoding; none resolved, ignored"
        )
    rewritten = _cursor_model(model, applied)
    if "[" in model and model.endswith("]"):
        note = f"reasoning effort via the cursor model-id bracket effort={applied.value}"
    else:
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
    """Encode cursor effort on a model id: update bracket ``effort=`` when present, else append ``-<tier>``.

    Compound Cursor ids (``name[effort=high,fast=true]``) keep non-effort bracket params and only rewrite the
    ``effort=`` entry (or insert one when missing). Identical effort leaves the id unchanged. Bare ids still
    use the ``-<tier>`` suffix. Guards: ``auto``, a ``-thinking`` variant, a bare ``-fast`` latency variant,
    and a model already ending in a reasoning-tier suffix are returned untouched.
    """
    lowered = model.lower()
    if model == "auto" or "thinking" in lowered:
        return model
    # * Compound bracket ids (Cursor dual-channel / Grok-style): update effort= inside [...], keep other params.
    if "[" in model and model.endswith("]"):
        return _cursor_bracket_effort(model, effort)
    if lowered.endswith("-fast"):
        return model
    if any(lowered.endswith(f"-{tier.value}") for tier in EFFORT_ORDER):
        return model
    return f"{model}-{effort.value}"


def _cursor_bracket_effort(model: str, effort: Effort) -> str:
    """Rewrite ``effort=...`` inside a Cursor compound bracket id, preserving sibling params."""
    base, _, rest = model.partition("[")
    params = rest[:-1]  # drop the trailing ']'
    parts = [part.strip() for part in params.split(",") if part.strip()]
    effort_value = effort.value
    replaced = False
    unchanged = False
    new_parts: list[str] = []
    for part in parts:
        key, sep, value = part.partition("=")
        if sep and key.strip().lower() == "effort":
            replaced = True
            if value.strip().lower() == effort_value:
                unchanged = True
                new_parts.append(part)
            else:
                new_parts.append(f"effort={effort_value}")
        else:
            new_parts.append(part)
    if unchanged:
        return model
    if not replaced:
        new_parts.insert(0, f"effort={effort_value}")
    return f"{base}[{','.join(new_parts)}]"


@dataclass(frozen=True, slots=True)
class CompoundModelId:
    """A parsed compound model id: bare ``base`` or ``base[key=value,...]`` (keys lowercased)."""

    base: str
    #: Normalized ``(key, value)`` pairs in advertisement order; keys are lowercased, values stripped.
    params: tuple[tuple[str, str], ...]


_BOOLEAN_PARAM_VALUES = frozenset({"true", "false"})


def parse_compound_model_id(model_id: str) -> CompoundModelId | None:
    """Parse a bare or bracketed compound model id; ``None`` when malformed (fail closed).

    Accepts ``base`` or ``base[k=v,k2=v2]``. Rejects empty base, unclosed / nested brackets, empty
    segments, missing ``=``, empty keys/values, and duplicate keys (case-insensitive). No vendor-specific
    model table -- structural parsing only.
    """
    if not model_id or model_id != model_id.strip():
        return None
    if "[" not in model_id:
        if "]" in model_id:
            return None
        return CompoundModelId(base=model_id, params=())
    if model_id.count("[") != 1 or not model_id.endswith("]"):
        return None
    base, _, rest = model_id.partition("[")
    if not base:
        return None
    params_str = rest[:-1]
    if not params_str:
        return CompoundModelId(base=base, params=())
    params: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in params_str.split(","):
        if not part.strip():
            return None
        key, sep, value = part.partition("=")
        if not sep:
            return None
        key_n = key.strip().lower()
        value_n = value.strip()
        if not key_n or not value_n or key_n in seen:
            return None
        seen.add(key_n)
        params.append((key_n, value_n))
    return CompoundModelId(base=base, params=tuple(params))


def launch_advertisement_compatible(requested: str, advertised: str) -> bool:
    """Whether ``requested`` may pass launch-only advertisement checks against ``advertised``.

    Exact string match wins. Otherwise both ids must parse, share the same base and the same param
    key set, match on every non-``fast`` value, and differ only in a boolean ``fast=`` value when they
    differ at all. Malformed either side, unknown base, or a differing ``effort`` (or any other param)
    returns ``False`` -- callers raise ``MODEL_UNAVAILABLE``. Does not rewrite either id.
    """
    if requested == advertised:
        return True
    left = parse_compound_model_id(requested)
    right = parse_compound_model_id(advertised)
    if left is None or right is None or left.base != right.base:
        return False
    left_map = dict(left.params)
    right_map = dict(right.params)
    if set(left_map) != set(right_map):
        return False
    differing = [key for key, value in left_map.items() if right_map[key] != value]
    if not differing:
        return True
    if differing != ["fast"]:
        return False
    return left_map["fast"].lower() in _BOOLEAN_PARAM_VALUES and right_map["fast"].lower() in _BOOLEAN_PARAM_VALUES


#: One agent's effort builder: ``(resolved model, tier)`` -> its override. The ``None`` effort case
#: short-circuits before dispatch, so a builder always receives a concrete tier.
_Builder = Callable[[str | None, Effort], EffortOverride]

#: The per-agent effort builders, keyed by agent id. An id absent here has no effort knob (a reported no-op).
_BUILDERS: dict[str, _Builder] = {
    "codex": _codex,
    "claude_code": _claude_code,
    "cursor": _cursor,
    "cline": _cline,
    "junie": _junie,
    "kiro": _kiro,
}

#: Config-option ids an agent uses to advertise its reasoning-effort tier over ACP. The session's
#: config-option effort path matches an advertised option by one of these ids (codex uses
#: ``reasoning_effort``, claude_code uses ``effort``), so a new agent that advertises one of them is covered
#: without a code change. Kept here, next to the builders, so the two halves of the effort wiring stay together.
EFFORT_CONFIG_OPTION_IDS: frozenset[str] = frozenset({"effort", "reasoning_effort"})


def clamp_to_supported(effort: Effort, supported: list[Effort]) -> Effort | None:
    """Clamp ``effort`` to the nearest tier at-or-below it within ``supported`` (the config-option path).

    ``supported`` is the agent's advertised effort options parsed to :class:`Effort` (order-agnostic). The
    requested tier wins if offered; otherwise the highest supported tier below it (so ``max`` on a codex
    option topping out at ``xhigh`` becomes ``xhigh``); if the request is below every supported tier, the
    lowest supported one. ``None`` only when nothing is supported (the caller then leaves effort a no-op).
    """
    if not supported:
        return None
    if effort in supported:
        return effort
    ordered = sorted(supported, key=EFFORT_ORDER.index)
    below = [tier for tier in ordered if EFFORT_ORDER.index(tier) < EFFORT_ORDER.index(effort)]
    return below[-1] if below else ordered[0]
