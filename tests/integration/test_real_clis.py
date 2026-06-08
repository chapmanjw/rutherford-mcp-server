# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Real-CLI integration tests. Local only, marked ``integration``, skipped in CI.

These exercise the actual CLI subprocesses end to end. Each skips unless its CLI is opted in
(``RUTHERFORD_IT_<CLI>=1``), installed, and authenticated. See docs/integration-testing.md.
"""

from __future__ import annotations

import pytest

from rutherford.context import AppContext
from rutherford.domain.enums import AuthState, Strategy
from rutherford.domain.models import (
    ConsensusRequest,
    ConsensusResult,
    DebateRequest,
    DelegationRequest,
    DelegationResult,
    StrategyResult,
    Target,
)

from .helpers import CLI_ENV, available_clis, skip_unless_available

pytestmark = pytest.mark.integration

_OK_PROMPT = "Reply with exactly the two characters: ok"


@pytest.mark.parametrize("cli_id", list(CLI_ENV))
async def test_read_only_delegation_returns_normalized_result(real_app: AppContext, cli_id: str) -> None:
    skip_unless_available(real_app, cli_id)
    request = DelegationRequest(target=Target(cli=cli_id), prompt=_OK_PROMPT, timeout_s=180)
    result = await real_app.delegation.delegate(request, base_depth=0)
    assert isinstance(result, DelegationResult)
    assert result.ok, f"{cli_id} delegation failed: {result.error}"
    assert result.text.strip()


@pytest.mark.parametrize("cli_id", list(CLI_ENV))
async def test_model_selection_is_honored_where_supported(real_app: AppContext, cli_id: str) -> None:
    skip_unless_available(real_app, cli_id)
    adapter = real_app.registry.get(cli_id)
    if not adapter.capabilities().supports_model_selection:
        pytest.skip(f"{cli_id} does not support model selection")
    models = adapter.available_models()
    if not models:
        pytest.skip(f"{cli_id} reported no selectable models")
    request = DelegationRequest(target=Target(cli=cli_id, model=models[0]), prompt=_OK_PROMPT, timeout_s=180)
    result = await real_app.delegation.delegate(request, base_depth=0)
    assert isinstance(result, DelegationResult)


@pytest.mark.parametrize("cli_id", list(CLI_ENV))
async def test_timeout_path_is_structured(real_app: AppContext, cli_id: str) -> None:
    skip_unless_available(real_app, cli_id)
    # A sub-second timeout forces the timeout path; the result must be structured, not an exception.
    request = DelegationRequest(target=Target(cli=cli_id), prompt=_OK_PROMPT, timeout_s=0.5)
    result = await real_app.delegation.delegate(request, base_depth=0)
    assert isinstance(result, DelegationResult)
    if not result.ok and result.error is not None:
        assert result.error.code in {"TIMEOUT", "NONZERO_EXIT", "PARSE_ERROR", "TRANSCRIPT_NOT_FOUND"}


async def test_self_invocation_and_depth_guard(real_app: AppContext) -> None:
    ready = available_clis(real_app)
    if not ready:
        pytest.skip("no CLI opted in for integration testing")
    cli_id = ready[0]
    # A CLI delegating to its own adapter is a fresh, isolated subprocess and returns normally.
    result = await real_app.delegation.delegate(
        DelegationRequest(target=Target(cli=cli_id), prompt=_OK_PROMPT, timeout_s=180),
        base_depth=0,
    )
    assert isinstance(result, DelegationResult)
    # The depth guard refuses a chain at the configured maximum, without spawning.
    refused = await real_app.delegation.delegate(
        DelegationRequest(target=Target(cli=cli_id), prompt=_OK_PROMPT),
        base_depth=real_app.config.max_depth,
    )
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code == "MAX_DEPTH_EXCEEDED"


async def test_multi_cli_consensus(real_app: AppContext) -> None:
    ready = available_clis(real_app)
    if len(ready) < 2:
        pytest.skip("need at least two opted-in CLIs for a multi-CLI consensus")
    targets = [Target(cli=cli_id) for cli_id in ready[:2]]
    result = await real_app.consensus.consensus(
        ConsensusRequest(targets=targets, prompt=_OK_PROMPT, timeout_s=180),
        base_depth=0,
    )
    assert isinstance(result, ConsensusResult)  # no strategy -> the legacy every-voice shape
    assert len(result.voices) == 2
    assert {voice.target.cli for voice in result.voices} == set(ready[:2])


def test_auth_state_is_reported_not_hung(real_app: AppContext) -> None:
    # doctor must report auth state for every adapter without hanging on a login prompt.
    for cli_id in CLI_ENV:
        auth = real_app.registry.get(cli_id).check_auth()
        assert auth.state in set(AuthState)


async def test_consensus_strategy_returns_a_sound_outcome(real_app: AppContext) -> None:
    # F1: a real strategy run aggregates live VERDICT lines. Every eligible voice is in the tally
    # with either a verdict or a recorded reason -- never a silent drop -- and the outcome is one of
    # the sound categories (a true majority, no_majority, or no_quorum).
    ready = available_clis(real_app)
    if len(ready) < 2:
        pytest.skip("need at least two opted-in CLIs for a strategy consensus")
    targets = [Target(cli=cli_id) for cli_id in ready[:3]]
    result = await real_app.consensus.consensus(
        ConsensusRequest(
            targets=targets,
            prompt="Is the integer 4 an even number? Answer in one short sentence.",
            strategy=Strategy.MAJORITY,
            timeout_s=180,
        ),
        base_depth=0,
    )
    assert isinstance(result, StrategyResult)
    assert len(result.voices) == len(targets)
    for voice in result.voices:
        assert voice.verdict is not None or voice.no_verdict_reason in {"failed", "unparseable"}
    assert result.outcome in {"majority", "no_majority", "no_quorum"}
    if result.outcome == "majority":
        assert result.decision  # a real winning token, e.g. "yes"


async def test_debate_two_same_cli_seats_do_not_collide(real_app: AppContext) -> None:
    # Seat-identity fix: two seats of the same CLI (same model) must not merge into one. Each gets a
    # distinct transcript label and a distinct identity.
    ready = available_clis(real_app)
    if not ready:
        pytest.skip("no CLI opted in for integration testing")
    cli_id = ready[0]
    result = await real_app.debate.debate(
        DebateRequest(
            targets=[Target(cli=cli_id), Target(cli=cli_id)],
            prompt="In one sentence: are tabs or spaces better for indentation, and why?",
            rounds=2,
            synthesize=False,
            timeout_s=180,
        ),
        base_depth=0,
    )
    round_one = result.rounds[0].contributions
    assert {c.label for c in round_one} == {cli_id, f"{cli_id}#2"}  # disambiguated, not merged
    assert len({c.seat_id for c in round_one}) == 2  # two distinct identities
