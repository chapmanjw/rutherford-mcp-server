# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Tests for F3: provider/model provenance derivation and effective-diversity reporting.

Covers the pure helpers, each adapter's provider rule (including the third-party-backend cases for
Claude Code and the open-weights-prefix trap for the local adapters), the service-side stamping of
provenance onto a delegation result, and the diversity computation surfaced on consensus/debate.
"""

from __future__ import annotations

import pytest

from rutherford.adapters.antigravity import AntigravityAdapter
from rutherford.adapters.claude_code import ClaudeCodeAdapter
from rutherford.adapters.codex import CodexAdapter
from rutherford.adapters.cursor import CursorAdapter
from rutherford.adapters.generic import GenericAdapter
from rutherford.adapters.goose import GooseAdapter
from rutherford.adapters.kiro import KiroAdapter
from rutherford.adapters.lmstudio import LMStudioAdapter
from rutherford.adapters.ollama import OllamaAdapter
from rutherford.adapters.opencode import OpenCodeAdapter
from rutherford.adapters.provenance import infer_provider_from_model, split_namespaced_model
from rutherford.adapters.qwen import QwenAdapter
from rutherford.adapters.registry import AdapterRegistry
from rutherford.config.schema import GenericAdapterConfig, RutherfordConfig
from rutherford.domain.models import (
    ConsensusRequest,
    DebateRequest,
    DelegationRequest,
    InvocationContext,
    ProcessResult,
    Provenance,
    Target,
)
from rutherford.services.consensus import ConsensusService
from rutherford.services.debate import DebateService
from rutherford.services.delegation import DelegationService
from rutherford.services.roles import load_roles
from rutherford.services.strategies import effective_diversity
from tests.fakes import FakeAdapter, FakeProbe, FakeProcessRunner

_BACKEND_SWITCHES = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)


@pytest.fixture(autouse=True)
def _clear_backend_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no CLAUDE_CODE_USE_* / GOOSE_PROVIDER ambient env leaking in."""
    for name in (*_BACKEND_SWITCHES, "GOOSE_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


def _ctx(cli: str, model: str | None) -> InvocationContext:
    return InvocationContext(target=Target(cli=cli, model=model), correlation_id="t")


# --- split_namespaced_model --------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-sonnet-4-6", ("anthropic", "claude-sonnet-4-6")),
        ("openai/gpt-5", ("openai", "gpt-5")),
        ("claude-opus", (None, "claude-opus")),  # no namespace
        ("a/b/c", ("a", "b/c")),  # only the first slash splits
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_namespaced_model(model: str | None, expected: tuple[str | None, str | None]) -> None:
    assert split_namespaced_model(model) == expected


# --- infer_provider_from_model -----------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4", "anthropic"),
        ("gpt-5.2", "openai"),
        ("o3-mini", "openai"),
        ("gemini-3-pro", "google"),
        ("gemma-4-12b", "google"),
        ("qwen3-coder", "alibaba"),
        ("grok-4", "xai"),
        ("mistral-large", "mistral"),
        ("devstral-2", "mistral"),
        ("llama-4", "meta"),
        ("deepseek-v3", "deepseek"),
        ("kimi-k2", "moonshot"),
        ("anthropic/claude-opus", "anthropic"),  # matched on the post-namespace tail
        ("us.anthropic.claude-3-5-sonnet-20241022-v2:0", "anthropic"),  # Bedrock region-prefixed id
        ("eu.meta.llama-3-70b", "meta"),  # region-prefixed, dotted vendor segment
        ("anthropic.claude-3-5-sonnet", "anthropic"),  # dotted, no region
        ("auto", None),  # Cursor's plan-agnostic id is not a vendor
        ("some-unknown-model", None),
        (None, None),
    ],
)
def test_infer_provider_from_model(model: str | None, expected: str | None) -> None:
    assert infer_provider_from_model(model) == expected


# --- per-adapter provider derivation -----------------------------------------


def test_claude_code_provider_is_anthropic_direct_by_default() -> None:
    prov = ClaudeCodeAdapter(FakeProbe()).provenance(_ctx("claude_code", "opus"))
    assert prov.provider == "anthropic"
    assert prov.model == "opus"
    assert prov.backend is None
    assert prov.confirmed is True


@pytest.mark.parametrize(
    ("switch", "expected_backend"),
    [
        ("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
        ("CLAUDE_CODE_USE_ANTHROPIC_AWS", "bedrock"),
        ("CLAUDE_CODE_USE_VERTEX", "vertex"),
        ("CLAUDE_CODE_USE_MANTLE", "mantle"),
    ],
)
def test_claude_code_backend_from_env_switch(
    monkeypatch: pytest.MonkeyPatch, switch: str, expected_backend: str
) -> None:
    monkeypatch.setenv(switch, "1")
    prov = ClaudeCodeAdapter(FakeProbe()).provenance(_ctx("claude_code", "opus"))
    assert prov.provider == "anthropic"  # the model maker is still Anthropic
    assert prov.backend == expected_backend


def test_claude_code_falsy_switch_is_not_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    assert ClaudeCodeAdapter(FakeProbe()).provenance(_ctx("claude_code", "opus")).backend is None


def test_codex_provider_is_openai_unconfirmed() -> None:
    prov = CodexAdapter(FakeProbe()).provenance(_ctx("codex", "gpt-5-codex"))
    assert prov.provider == "openai"
    assert prov.confirmed is False  # could be amazon-bedrock; not probed in the hot path


def test_unconfirmed_home_default_yields_to_model_evidence() -> None:
    # Codex's home vendor (openai) is unconfirmed; a model id that clearly names another vendor wins,
    # so a Codex run pointed at a Bedrock-Anthropic model is not mislabeled openai.
    prov = CodexAdapter(FakeProbe()).provenance(_ctx("codex", "anthropic.claude-3-5-sonnet"))
    assert prov.provider == "anthropic"


def test_unconfirmed_home_default_used_when_model_is_uninformative() -> None:
    prov = CodexAdapter(FakeProbe()).provenance(_ctx("codex", None))
    assert prov.provider == "openai"  # the home default, since the model reveals nothing
    assert prov.confirmed is False


def test_antigravity_reports_google_and_its_fixed_model() -> None:
    # No selector, but the print-mode model is fixed and known -> report it, not "unknown".
    prov = AntigravityAdapter(FakeProbe()).provenance(_ctx("antigravity", None))
    assert prov.provider == "google"
    assert prov.model == "gemini-3.5-flash"
    assert prov.confirmed is True


def test_antigravity_ignores_a_requested_model() -> None:
    # agy -p has no model selector; a caller-supplied model never runs, so provenance must not
    # confirm it -- it reports the model that actually answered.
    prov = AntigravityAdapter(FakeProbe()).provenance(_ctx("antigravity", "gpt-5"))
    assert prov.model == "gemini-3.5-flash"


def test_cursor_provider_is_inferred_from_the_model() -> None:
    cursor = CursorAdapter(FakeProbe())
    assert cursor.provenance(_ctx("cursor", "gpt-5.2")).provider == "openai"
    assert cursor.provenance(_ctx("cursor", "claude-sonnet")).provider == "anthropic"
    # "auto" (every-plan id) is not a vendor -> unknown, unconfirmed.
    auto = cursor.provenance(_ctx("cursor", "auto"))
    assert auto.provider is None
    assert auto.confirmed is False


def test_opencode_provider_from_namespace_prefix() -> None:
    prov = OpenCodeAdapter(FakeProbe()).provenance(_ctx("opencode", "anthropic/claude-sonnet-4-6"))
    assert prov.provider == "anthropic"
    assert prov.model == "claude-sonnet-4-6"  # the bare model id, prefix stripped
    assert prov.confirmed is True


def test_opencode_bare_model_falls_back_to_heuristic() -> None:
    prov = OpenCodeAdapter(FakeProbe()).provenance(_ctx("opencode", "gpt-5"))
    assert prov.provider == "openai"
    assert prov.confirmed is False


def test_opencode_serving_platform_prefix_goes_to_backend_axis() -> None:
    # A models.dev gateway/cloud prefix is a serving backend, not a vendor: it must not be reported
    # as a confirmed provider (that would pollute the distinct-provider diversity count).
    prov = OpenCodeAdapter(FakeProbe()).provenance(_ctx("opencode", "amazon-bedrock/anthropic.claude-3-5-sonnet"))
    assert prov.backend == "bedrock"
    assert prov.provider == "anthropic"  # inferred from the model tail, not the namespace
    assert prov.model == "anthropic.claude-3-5-sonnet"
    assert prov.confirmed is False


def test_opencode_openrouter_prefix_is_a_backend() -> None:
    prov = OpenCodeAdapter(FakeProbe()).provenance(_ctx("opencode", "openrouter/openai/gpt-5"))
    assert prov.backend == "openrouter"
    assert prov.provider == "openai"  # from the tail "openai/gpt-5"
    assert prov.confirmed is False


def test_opencode_inference_gateway_prefix_is_a_backend() -> None:
    # An inference provider (groq/together/fireworks) serves open-weights models; it is a backend,
    # and the vendor is inferred from the model id (Meta makes llama).
    prov = OpenCodeAdapter(FakeProbe()).provenance(_ctx("opencode", "groq/llama-3.3-70b"))
    assert prov.backend == "groq"
    assert prov.provider == "meta"
    assert prov.confirmed is False


def test_opencode_local_runtime_prefix_is_the_local_sentinel() -> None:
    # An OpenCode ollama/lmstudio model is served locally -> the same `local` provider the dedicated
    # local adapters report, not a cloud backend.
    prov = OpenCodeAdapter(FakeProbe()).provenance(_ctx("opencode", "ollama/gemma3:12b"))
    assert prov.provider == "local"
    assert prov.backend is None
    assert prov.model == "gemma3:12b"
    assert prov.confirmed is True


def test_opencode_bedrock_region_prefixed_model_infers_vendor() -> None:
    prov = OpenCodeAdapter(FakeProbe()).provenance(
        _ctx("opencode", "amazon-bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0")
    )
    assert prov.backend == "bedrock"
    assert prov.provider == "anthropic"  # inferred through the region prefix


def test_goose_provider_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOSE_PROVIDER", "Anthropic")
    prov = GooseAdapter(FakeProbe()).provenance(_ctx("goose", "claude-x"))
    assert prov.provider == "anthropic"  # normalized to lower
    assert prov.confirmed is True


def test_goose_without_env_uses_model_heuristic() -> None:
    prov = GooseAdapter(FakeProbe()).provenance(_ctx("goose", "gpt-5"))
    assert prov.provider == "openai"
    assert prov.confirmed is False


def test_goose_serving_platform_env_goes_to_backend_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    # GOOSE_PROVIDER values include serving platforms (bedrock, databricks, azure); those are the
    # backend, with the vendor inferred from the model -- not a confident "bedrock" provider.
    monkeypatch.setenv("GOOSE_PROVIDER", "bedrock")
    prov = GooseAdapter(FakeProbe()).provenance(_ctx("goose", "claude-sonnet-4"))
    assert prov.backend == "bedrock"
    assert prov.provider == "anthropic"  # inferred from the model id
    assert prov.confirmed is False


def test_qwen_provider_is_alibaba_unconfirmed() -> None:
    prov = QwenAdapter(FakeProbe()).provenance(_ctx("qwen", "qwen3-coder"))
    assert prov.provider == "alibaba"
    assert prov.confirmed is False


def test_kiro_serves_via_aws_backend_with_unknown_vendor() -> None:
    # AWS is a serving platform, not a vendor: it belongs on the backend axis so the same underlying
    # model reached directly is not double-counted as a different provider.
    prov = KiroAdapter(FakeProbe()).provenance(_ctx("kiro", "kiro-default"))
    assert prov.provider is None
    assert prov.backend == "aws"
    assert prov.confirmed is False


def test_local_adapters_are_local_and_do_not_split_the_org_prefix() -> None:
    # The model key's org prefix (google/) is open-weights origin, NOT a cloud provider.
    ollama = OllamaAdapter(FakeProbe()).provenance(_ctx("ollama", "gemma3:12b"))
    assert ollama.provider == "local"
    assert ollama.confirmed is True
    lms = LMStudioAdapter(FakeProbe()).provenance(_ctx("lmstudio", "google/gemma-4-12b"))
    assert lms.provider == "local"
    assert lms.model == "google/gemma-4-12b"  # kept whole, not split to "google"


def test_generic_provider_from_config() -> None:
    cfg = GenericAdapterConfig(id="gen", display_name="Gen", binary="gen", provider="openai", natively_read_only=True)
    prov = GenericAdapter(cfg, probe=FakeProbe()).provenance(_ctx("gen", "some-model"))
    assert prov.provider == "openai"
    assert prov.confirmed is True


def test_generic_without_config_provider_uses_heuristic() -> None:
    cfg = GenericAdapterConfig(id="gen", display_name="Gen", binary="gen", natively_read_only=True)
    prov = GenericAdapter(cfg, probe=FakeProbe()).provenance(_ctx("gen", "claude-x"))
    assert prov.provider == "anthropic"
    assert prov.confirmed is False


# --- service-side stamping ---------------------------------------------------


def _delegation(adapters: list[FakeAdapter], runner: FakeProcessRunner) -> DelegationService:
    cfg = RutherfordConfig()
    return DelegationService(AdapterRegistry(adapters), runner, cfg, load_roles())


async def test_delegation_stamps_provenance_with_cli_version() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="hi"))
    service = _delegation([FakeAdapter("fake", provider="anthropic")], runner)
    result = await service.delegate(DelegationRequest(target=Target(cli="fake", model="opus"), prompt="q"))
    assert result.provenance is not None
    assert result.provenance.provider == "anthropic"
    assert result.provenance.model == "opus"
    assert result.provenance.cli_version == "1.0.0"  # from FakeAdapter.detect()
    assert result.provenance.confirmed is True


async def test_delegation_provenance_present_even_when_provider_unknown() -> None:
    # No provider, but the CLI version is known -> the block is still worth keeping.
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="hi"))
    service = _delegation([FakeAdapter("fake")], runner)
    result = await service.delegate(DelegationRequest(target=Target(cli="fake", model="m1"), prompt="q"))
    assert result.provenance is not None
    assert result.provenance.provider is None
    assert result.provenance.cli_version == "1.0.0"


async def test_delegation_provenance_absent_when_binary_not_installed() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="hi"))
    service = _delegation([FakeAdapter("fake", installed=False)], runner)
    result = await service.delegate(DelegationRequest(target=Target(cli="fake"), prompt="q"))
    assert not result.ok
    assert result.provenance is None  # nothing ran, no provenance to report


# --- effective_diversity (pure) ----------------------------------------------


def _prov(provider: str | None = None, model: str | None = None, backend: str | None = None) -> Provenance:
    return Provenance(provider=provider, model=model, backend=backend)


def test_diversity_all_distinct() -> None:
    report = effective_diversity([_prov("anthropic", "opus"), _prov("openai", "gpt-5"), _prov("google", "gemini")])
    assert report.distinct_models == 3
    assert report.distinct_providers == 3
    assert report.answered_voices == 3
    assert report.low_diversity is False
    assert report.models == ["gemini", "gpt-5", "opus"]


def test_diversity_all_same_model_is_flagged() -> None:
    report = effective_diversity([_prov("anthropic", "opus"), _prov("anthropic", "opus")])
    assert report.distinct_models == 1
    assert report.low_diversity is True


def test_diversity_case_insensitive_model_dedup() -> None:
    report = effective_diversity([_prov("anthropic", "Opus"), _prov("anthropic", "opus")])
    assert report.distinct_models == 1


def test_diversity_same_model_different_backend_is_one_provider() -> None:
    # opus direct vs opus on Bedrock: one model, one provider (the vendor), the backend differs.
    report = effective_diversity([_prov("anthropic", "opus"), _prov("anthropic", "opus", backend="bedrock")])
    assert report.distinct_models == 1
    assert report.distinct_providers == 1


def test_diversity_same_vendor_different_model_strings_is_flagged() -> None:
    # The load-bearing case: two Anthropic models with different id strings are NOT independent. The
    # model axis misses it (two strings), the provider axis catches it (one vendor).
    report = effective_diversity([_prov("anthropic", "opus"), _prov("anthropic", "claude-opus-4")])
    assert report.distinct_models == 2
    assert report.distinct_providers == 1
    assert report.low_diversity is True


def test_diversity_unknown_provider_does_not_false_flag() -> None:
    # Two genuinely different models, one with an unresolved provider: not flagged (the provider axis
    # only fires when at least two providers are known and collapse).
    report = effective_diversity([_prov("anthropic", "opus"), _prov(None, "gpt-5")])
    assert report.distinct_models == 2
    assert report.low_diversity is False


def test_diversity_unknown_voices_are_bucketed_not_flagged() -> None:
    report = effective_diversity([_prov(None, None), _prov(None, None)])
    assert report.unknown == 2
    assert report.distinct_models == 0
    assert report.low_diversity is False  # all-unknown is unmeasured, not low


def test_diversity_known_collapse_with_one_unknown_is_flagged() -> None:
    report = effective_diversity([_prov("anthropic", "opus"), _prov("anthropic", "opus"), _prov(None, None)])
    assert report.distinct_models == 1
    assert report.unknown == 1
    assert report.low_diversity is True  # two known voices collapsed to one model


def test_diversity_min_distinct_threshold() -> None:
    provs = [_prov("anthropic", "opus"), _prov("openai", "gpt-5")]
    assert effective_diversity(provs, min_distinct=2).low_diversity is False
    assert effective_diversity(provs, min_distinct=3).low_diversity is True


def test_diversity_single_voice_never_flags() -> None:
    report = effective_diversity([_prov("anthropic", "opus")])
    assert report.low_diversity is False


def test_diversity_empty_panel() -> None:
    report = effective_diversity([])
    assert report.answered_voices == 0
    assert report.distinct_models == 0
    assert report.low_diversity is False


# --- diversity surfaced on consensus / debate --------------------------------


def _consensus(adapters: list[FakeAdapter], runner: FakeProcessRunner) -> ConsensusService:
    cfg = RutherfordConfig()
    registry = AdapterRegistry(adapters)
    return ConsensusService(DelegationService(registry, runner, cfg, load_roles()), cfg, registry)


async def test_consensus_reports_high_diversity() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a", provider="anthropic"), FakeAdapter("b", provider="openai")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a", model="opus"), Target(cli="b", model="gpt-5")], prompt="q")
    )
    assert result.diversity is not None
    assert result.diversity.distinct_models == 2
    assert result.diversity.distinct_providers == 2
    assert result.diversity.low_diversity is False


async def test_consensus_flags_one_model_in_two_costumes() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    service = _consensus([FakeAdapter("a", provider="anthropic"), FakeAdapter("b", provider="anthropic")], runner)
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a", model="opus"), Target(cli="b", model="opus")], prompt="q")
    )
    assert result.diversity is not None
    assert result.diversity.distinct_models == 1
    assert result.diversity.low_diversity is True


async def test_consensus_diversity_excludes_failed_voices() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    # "b" is not installed -> its voice fails and is excluded from the diversity tally.
    service = _consensus(
        [FakeAdapter("a", provider="anthropic"), FakeAdapter("b", provider="openai", installed=False)], runner
    )
    result = await service.consensus(
        ConsensusRequest(targets=[Target(cli="a", model="opus"), Target(cli="b", model="gpt-5")], prompt="q")
    )
    assert result.diversity is not None
    assert result.diversity.answered_voices == 1  # only "a" answered
    assert result.diversity.distinct_models == 1


async def test_strategy_result_carries_diversity_and_per_voice_provenance() -> None:
    from rutherford.domain.enums import Strategy

    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="answer\nVERDICT: yes"))
    service = _consensus([FakeAdapter("a", provider="anthropic"), FakeAdapter("b", provider="openai")], runner)
    result = await service.consensus(
        ConsensusRequest(
            targets=[Target(cli="a", model="opus"), Target(cli="b", model="gpt-5")],
            prompt="q",
            strategy=Strategy.MAJORITY,
        )
    )
    assert result.diversity is not None
    assert result.diversity.distinct_models == 2
    assert all(voice.provenance is not None for voice in result.voices)
    assert {voice.provenance.provider for voice in result.voices if voice.provenance} == {"anthropic", "openai"}


async def test_debate_reports_diversity_over_final_round() -> None:
    runner = FakeProcessRunner(ProcessResult(exit_code=0, stdout="ok"))
    cfg = RutherfordConfig()
    registry = AdapterRegistry([FakeAdapter("a", provider="anthropic"), FakeAdapter("b", provider="anthropic")])
    debate = DebateService(DelegationService(registry, runner, cfg, load_roles()), cfg)
    result = await debate.debate(
        DebateRequest(
            targets=[Target(cli="a", model="opus"), Target(cli="b", model="opus")],
            prompt="q",
            rounds=1,
            synthesize=False,
        )
    )
    assert result.diversity is not None
    assert result.diversity.distinct_models == 1
    assert result.diversity.low_diversity is True


def test_short_openai_prefixes_do_not_match_unrelated_segments() -> None:
    # o1/o3/o4 are matched as whole tokens, not bare prefixes: a region, tag, or version segment
    # that merely BEGINS with those characters must not mis-infer openai (it would inflate the
    # panel's distinct-provider count with a phantom vendor).
    assert infer_provider_from_model("custom:o3x-experimental") is None
    assert infer_provider_from_model("vendor/o1processor") is None
    assert infer_provider_from_model("foo.o4ward.bar") is None
    # The real families still resolve, wherever the token sits.
    assert infer_provider_from_model("o1") == "openai"
    assert infer_provider_from_model("o3-mini") == "openai"
    assert infer_provider_from_model("azure/o4-mini-high") == "openai"
