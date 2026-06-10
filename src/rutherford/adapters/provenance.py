# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
"""Shared helpers for deriving a delegation's provider/model provenance (F3).

An adapter knows best who served its answer, but the *rules* for turning a model string into a
vendor name, or splitting a ``provider/model`` namespace, recur across adapters. Those pure helpers
live here so each adapter composes them instead of carrying its own copy, and so the heuristic that
guesses a vendor from a model id has one definition to audit.

The :class:`~rutherford.domain.models.Provenance` model itself lives in ``domain/models.py`` (it is
part of the result envelope); this module is only the derivation logic.
"""

from __future__ import annotations

import re

from ..domain.models import Provenance

#: Common provider name constants, so adapters and the heuristic share one spelling.
ANTHROPIC = "anthropic"
OPENAI = "openai"
GOOGLE = "google"
ALIBABA = "alibaba"
XAI = "xai"
MISTRAL = "mistral"
META = "meta"
DEEPSEEK = "deepseek"
MOONSHOT = "moonshot"
LOCAL = "local"

#: Model-name prefixes mapped to the vendor that makes the model, longest/most-specific first within
#: a vendor. Matched against the model id (and its post-namespace tail) lowercased. Deliberately a
#: heuristic: it recognizes the common families and returns ``None`` for anything it does not know,
#: so an unrecognized model degrades to "unknown" rather than a wrong guess.
_MODEL_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", ANTHROPIC),
    ("anthropic", ANTHROPIC),
    ("gpt", OPENAI),
    ("openai", OPENAI),
    ("codex", OPENAI),
    ("gemini", GOOGLE),
    ("gemma", GOOGLE),
    ("google", GOOGLE),
    ("qwen", ALIBABA),
    ("grok", XAI),
    ("mistral", MISTRAL),
    ("devstral", MISTRAL),
    ("codestral", MISTRAL),
    ("llama", META),
    ("deepseek", DEEPSEEK),
    ("kimi", MOONSHOT),
)


def split_namespaced_model(model: str | None) -> tuple[str | None, str | None]:
    """Split a ``provider/model`` string into ``(provider, model)``; ``(None, model)`` when not namespaced.

    For a CLI whose model selector encodes the provider (OpenCode's ``-m anthropic/claude-sonnet-4-6``),
    the prefix before the first ``/`` is the provider and the rest is the model id. A bare model with
    no ``/`` returns ``(None, model)`` -- the caller falls back to the model-name heuristic. Only the
    first ``/`` splits, so a model id that itself contains slashes keeps them.
    """
    if not model:
        return None, None
    if "/" not in model:
        return None, model
    provider, _, rest = model.partition("/")
    provider = provider.strip().lower()
    return (provider or None), (rest or None)


#: Splits a model id into vendor-bearing segments so a vendor token is found wherever it sits: after
#: a ``provider/`` namespace, after a Bedrock region/inference-profile prefix (``us.anthropic.claude``),
#: or after a ``:`` tag. Not split on ``-`` so a family token (``claude-opus``) stays whole.
_SEGMENT_SPLIT = re.compile(r"[/.:]")

#: The short, collision-prone OpenAI families (o1/o3/o4) matched as WHOLE tokens: a segment must be
#: the family itself, optionally followed by a ``-variant`` or digits (``o1``, ``o3-mini``,
#: ``o4-mini-high``). A bare startswith for two-character prefixes would let an unrelated region,
#: tag, or version segment beginning ``o1``/``o3``/``o4`` mis-infer ``openai`` and skew the panel's
#: diversity accounting.
_SHORT_OPENAI_TOKEN = re.compile(r"^o[134](?:$|[-\d])")


def infer_provider_from_model(model: str | None) -> str | None:
    """Guess the model's vendor from its id, or ``None`` when unrecognized.

    A fallible heuristic for CLIs that neither fix a vendor nor namespace their model (Cursor, an
    unconfigured generic adapter, or a model id behind a serving backend). It matches the known family
    prefixes against each ``/``-, ``.``-, or ``:``-delimited segment, so a vendor token is found even
    when it is preceded by a namespace or a Bedrock region prefix (``us.anthropic.claude-...`` ->
    ``anthropic``). The two-character OpenAI families (o1/o3/o4) are matched as whole tokens, not
    prefixes -- see :data:`_SHORT_OPENAI_TOKEN`. Returns ``None`` for anything it does not recognize
    so the caller reports an honest "unknown" instead of a wrong vendor.
    """
    if not model:
        return None
    lowered = model.lower()
    segments = _SEGMENT_SPLIT.split(lowered)
    for prefix, provider in _MODEL_PROVIDER_PREFIXES:
        if any(segment.startswith(prefix) for segment in segments):
            return provider
    if any(_SHORT_OPENAI_TOKEN.match(segment) for segment in segments):
        return OPENAI
    return None


#: A CLI's "provider" namespace (OpenCode's models.dev ``provider/model`` prefix, Goose's
#: ``GOOSE_PROVIDER``) mixes true model vendors with SERVING PLATFORMS -- the clouds, gateways, and
#: inference providers that front someone else's models. Those must land on the ``backend`` axis,
#: never reported as the vendor, or "the same model served two ways" inflates the distinct-provider
#: count. Maps each known alias (models.dev and Goose spellings, hyphenated and snake/flat) to its
#: canonical backend name. A maintained denylist: an unknown prefix is taken as a vendor, so a new
#: gateway must be added here to be classed correctly.
_BACKEND_ALIASES: dict[str, str] = {
    # cloud model platforms
    "amazon-bedrock": "bedrock",
    "bedrock": "bedrock",
    "google-vertex": "vertex",
    "vertex": "vertex",
    "gcp_vertex_ai": "vertex",
    "vertex_ai": "vertex",
    "azure": "azure",
    "azure-openai": "azure",
    "databricks": "databricks",
    "sagemaker": "sagemaker",
    "sagemaker_tgi": "sagemaker",
    "mantle": "mantle",
    # aggregators / proxies
    "openrouter": "openrouter",
    "litellm": "litellm",
    "requesty": "requesty",
    # inference providers (serve open-weights models)
    "groq": "groq",
    "together": "together",
    "togetherai": "together",
    "fireworks": "fireworks",
    "fireworks-ai": "fireworks",
    "deepinfra": "deepinfra",
    "cerebras": "cerebras",
    "hyperbolic": "hyperbolic",
    "nebius": "nebius",
    "baseten": "baseten",
    # coding-tool gateways (both the hyphenated models.dev id and Goose's flat spelling)
    "github-copilot": "github-copilot",
    "githubcopilot": "github-copilot",
    "github-models": "github-models",
}

#: Local-runtime namespaces: an OpenCode ``ollama/...`` / ``lmstudio/...`` model is served on this
#: machine, so its provider is the ``local`` sentinel -- the same axis the dedicated Ollama/LM Studio
#: adapters use -- not a cloud backend. Kept distinct from :data:`_BACKEND_ALIASES` so the two local
#: code paths agree.
_LOCAL_RUNTIMES: frozenset[str] = frozenset({"ollama", "lmstudio", "llamacpp", "local"})


def classify_provider_namespace(name: str | None) -> tuple[str | None, str | None]:
    """Classify a CLI's provider id/namespace into ``(vendor, backend)``.

    A known serving platform (``amazon-bedrock``, ``openrouter``, ``groq``, ...) is returned as a
    backend with no vendor (the vendor is left to the model id). Anything else is taken as the vendor.
    Both OpenCode and Goose use namespaces that mix the two, so neither can treat the prefix as a
    vendor unconditionally. (A local-runtime namespace is handled by :func:`provenance_from_namespace`
    before this is consulted.)
    """
    if not name:
        return None, None
    key = name.strip().lower()
    if key in _BACKEND_ALIASES:
        return None, _BACKEND_ALIASES[key]
    return key, None


def provenance_from_namespace(namespace: str | None, model: str | None) -> Provenance:
    """Build provenance for a CLI whose provider comes from a ``provider/model`` namespace or env value.

    Shared by OpenCode (the ``-m`` prefix) and Goose (``GOOSE_PROVIDER``). A local-runtime namespace is
    the ``local`` provider sentinel (matching the dedicated local adapters); a known serving platform
    becomes the ``backend`` with the vendor inferred from the model id (unconfirmed -- a heuristic); a
    true-vendor namespace is a confirmed provider; an absent/unrecognized namespace falls back to the
    model-name heuristic. ``cli_version`` is filled in by the service.
    """
    if namespace and namespace.strip().lower() in _LOCAL_RUNTIMES:
        return Provenance(provider=LOCAL, model=model, confirmed=True)
    vendor, backend = classify_provider_namespace(namespace)
    if backend is not None:
        return Provenance(provider=infer_provider_from_model(model), backend=backend, model=model, confirmed=False)
    if vendor is not None:
        return Provenance(provider=vendor, model=model, confirmed=True)
    return Provenance(provider=infer_provider_from_model(model), model=model, confirmed=False)
