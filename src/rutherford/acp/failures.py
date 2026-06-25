# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""The ACP failure taxonomy (F7): decide what a failed turn means for cooldown and for model fallback.

Under ACP a turn already fails with a *structured* code (``ACP_SPAWN_FAILED`` / ``ACP_HANDSHAKE_FAILED`` /
``ACP_TURN_TIMEOUT`` / ``ACP_REFUSED`` / ``ACP_EMPTY_ANSWER`` / ``ACP_TURN_ERROR``) and a classified
:class:`~rutherford.domain.enums.ReexecutionSafety`, so there is no stdout marker-matching to do for the
*retry* decision -- the SAFE gate in the delegation service replaces v2's ``is_retryable`` taxonomy. This
module answers the two questions that gate is not enough for:

* :func:`indicates_unhealthy` -- does this failure suggest the *agent seat itself* is broken (down,
  mis-launching, hung, throttled, auth-dead), so it should count toward the agent's cooldown? A clean
  refusal, an empty answer, or a bad-prompt error is the request's fault, not the seat's, and must NOT bench
  a healthy agent.
* :func:`is_model_unavailable` -- does a failure message look like "this model is not available to you",
  the one case that drives a same-agent retry on a configured fallback model?

Pure and dependency-light, so the classification is unit-testable on its own.
"""

from __future__ import annotations

from ..domain.error_codes import ErrorCode

#: ACP failure codes that suggest the agent *seat* itself is broken (failed to launch, failed the
#: handshake, hung past its timeout, or dropped its connection mid-turn), so the failure should count
#: toward the agent's cooldown. Deliberately EXCLUDES the post-prompt "the request was bad / the model
#: declined" outcomes -- ``ACP_REFUSED`` (a clean refusal) and ``ACP_EMPTY_ANSWER`` (no answer text) -- which
#: a healthy agent returns on a hard or disallowed prompt; benching a healthy agent on those is the
#: feature's most likely false positive. The v2 rate-limit / auth classes (``RATE_LIMITED`` / ``AUTH_FAILED``)
#: are kept in the set so a refinement that maps an in-turn throttle/auth rejection to one of them benches the
#: seat too, even though ACP does not surface them natively today.
_UNHEALTHY: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.ACP_SPAWN_FAILED,
        ErrorCode.ACP_HANDSHAKE_FAILED,
        ErrorCode.ACP_TURN_TIMEOUT,
        ErrorCode.ACP_TURN_ERROR,
        ErrorCode.RATE_LIMITED,
        ErrorCode.AUTH_FAILED,
    }
)

#: Markers a failure is "this model is not available to you" rather than a real error (matched
#: case-insensitively as substrings of the error message). The cost of a false positive is one extra retry
#: on the agent's configured fallback model. Ported from v2's ``_MODEL_UNAVAILABLE_MARKERS``.
_MODEL_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "named models unavailable",
    "switch to auto",
    "only use auto",
    "model is not available",
    "model not available",
    "model unavailable",
    "model_unavailable",
    "no access to model",
    "not available on your plan",
    "upgrade your plan",
    "upgrade plans to continue",
    "unknown model",
    "invalid model",
    # AWS Bedrock / Vertex reject a model id the provider does not offer with this phrasing (note the word
    # order differs from "invalid model" above, so it needs its own marker): e.g. a Claude Code on Bedrock
    # handed the bare cloud alias "claude-opus-4-8" -> "The provided model identifier is invalid.".
    "model identifier is invalid",
    "provided model identifier",
)


def indicates_unhealthy(code: ErrorCode | str) -> bool:
    """Whether a failure with ``code`` should count toward the agent's cooldown.

    ``True`` for a transport / handshake / timeout / throttle / auth failure (the seat is broken); ``False``
    for a clean refusal, an empty answer, a bad-prompt or guard error, or any non-ACP code -- those are the
    request's fault and must not bench a healthy agent.
    """
    return code in _UNHEALTHY


def is_model_unavailable(message: str) -> bool:
    """Whether ``message`` looks like a model-availability rejection (drives same-agent model fallback)."""
    lowered = message.lower()
    return any(marker in lowered for marker in _MODEL_UNAVAILABLE_MARKERS)
