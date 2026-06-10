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
from rutherford.tools.probing import version_token

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
    # F3: a real, successful run stamps provenance with at least one resolved axis -- a provider (a
    # fixed-vendor adapter) and/or the detected CLI version. A BYOK adapter on a no-model default run
    # may not resolve a provider, which is the honest "unknown" rather than a guess.
    assert result.provenance is not None, f"{cli_id} produced no provenance"
    assert result.provenance.provider or result.provenance.cli_version, f"{cli_id} provenance is empty"


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


async def test_consensus_reports_effective_diversity(real_app: AppContext) -> None:
    # F3: a real multi-CLI panel surfaces a diversity report built from each voice's live provenance,
    # so distinct providers/models reflect the actual mix that answered.
    ready = available_clis(real_app)
    if len(ready) < 2:
        pytest.skip("need at least two opted-in CLIs for a diversity panel")
    targets = [Target(cli=cli_id) for cli_id in ready[:3]]
    result = await real_app.consensus.consensus(
        ConsensusRequest(targets=targets, prompt=_OK_PROMPT, timeout_s=180),
        base_depth=0,
    )
    assert isinstance(result, ConsensusResult)
    answered = [voice for voice in result.voices if voice.ok]
    if not answered:
        pytest.skip("no voice answered; cannot assert diversity")
    assert result.diversity is not None
    assert result.diversity.answered_voices == len(answered)
    # Real provenance flowed through: at least one provider was resolved across the answering panel.
    assert result.diversity.distinct_providers >= 1
    assert result.diversity.providers


async def test_cross_target_fallback_recovers_live(real_app: AppContext) -> None:
    # F7: a real retryable failure recovers via the cross-target chain. The primary is a ready CLI
    # given a bogus model so the provider rejects it (a retryable failure); it has no same-adapter
    # model fallback, so the chain reaches a different, working CLI and records the path.
    ready = available_clis(real_app)
    if len(ready) < 2:
        pytest.skip("need at least two opted-in CLIs for a cross-target fallback")
    # A primary whose adapter offers no model fallback, so a bad model is not rescued in-adapter.
    primary = next((c for c in ready if real_app.registry.get(c).fallback_model() is None), None)
    survivor = next((c for c in ready if c != primary), None)
    if primary is None or survivor is None:
        pytest.skip("need a no-model-fallback primary and a distinct survivor")
    result = await real_app.delegation.delegate(
        DelegationRequest(
            target=Target(cli=primary, model="rutherford-nonexistent-model-zzz"),
            prompt=_OK_PROMPT,
            fallback=[Target(cli=survivor)],
            timeout_s=180,
        ),
        base_depth=0,
    )
    assert result.ok, f"cross-target fallback did not recover: {result.error}"
    assert result.target.cli == survivor  # a different CLI answered
    assert result.fallback_chain  # the failed primary is recorded in the path


async def test_antigravity_version_matches_the_pin(real_app: AppContext) -> None:
    # Drift alarm: agy auto-updates, and its transcript layout is reverse-engineered and pinned. When
    # the running version moves past the pin this fails loudly, prompting a re-verify + re-pin -- far
    # better than a flood of TRANSCRIPT_NOT_FOUND during the June-18 Gemini -> agy migration wave.
    skip_unless_available(real_app, "antigravity")
    adapter = real_app.registry.get("antigravity")
    version = version_token(adapter.detect().version)
    pinned = version_token(getattr(adapter, "verified_version", None))
    assert version == pinned, (
        f"agy is at {adapter.detect().version} but the antigravity adapter is verified/pinned at "
        f"{getattr(adapter, 'verified_version', None)} -- re-verify the brain/ transcript layout and "
        "update verified_version + the docstring"
    )


async def test_antigravity_print_stdout_still_does_not_carry_the_answer(real_app: AppContext) -> None:
    # Stdout-recovery watch: agy --print emits nothing usable to stdout under a non-TTY pipe (issue
    # #76), which is why the adapter reads the transcript. The canary answer is the LOWERCASE of an
    # UPPERCASE token in the prompt, so a prompt echo on stdout/stderr cannot trip it -- only the
    # model's real answer landing on stdout does. When this FAILS, agy has started carrying the answer
    # on stdout -- the signal that the transcript archaeology can be retired.
    skip_unless_available(real_app, "antigravity")
    request = DelegationRequest(
        target=Target(cli="antigravity"),
        prompt="Output only the single token RUTHERFORDCANARY in lowercase, with no other text.",
        include_raw=True,
        timeout_s=300,
    )
    result = await real_app.delegation.delegate(request, base_depth=0)
    if not result.ok:
        pytest.skip(f"antigravity did not answer ({result.error}); cannot check stdout")
    assert "rutherfordcanary" not in (result.raw or ""), (
        "agy --print now carries the answer on its stdout/stderr -- issue #76 may be fixed; consider "
        "switching the antigravity adapter from the transcript read to clean stdout"
    )
