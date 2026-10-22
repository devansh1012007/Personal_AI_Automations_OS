"""Provider-agnostic inference layer.

The one-line summary of ADR-0007 and ADR-0015: the runtime does not care what
model is behind the interface, and swapping in a more capable one is a
configuration change rather than a rewrite.

Typical wiring::

    from paa.config import get_settings
    from paa.models import get_model_router

    router = get_model_router(get_settings(), ledger_store=ledger)
    response = await router.complete(
        request,
        modality=ComplexityModality.COMPLEX,
        permission_mode=settings.policy.mode,
        correlation_id=correlation_id,
        reason="multi-file refactor plan",
    )
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from paa.models.anthropic_provider import AnthropicProvider
from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    MalformedSchemaError,
    Message,
    ModelProvider,
    ModelTier,
    ModelUnavailableError,
    StructuredOutputError,
    extract_json_object,
    minimal_instance_for_schema,
    redact,
    redact_detail,
    validate_against_schema,
)
from paa.models.echo_provider import EchoProvider
from paa.models.embeddings import get_embedder
from paa.models.gemini_provider import GeminiProvider
from paa.models.ollama_provider import OllamaProvider
from paa.models.openai_compatible_provider import OpenAICompatibleProvider
from paa.models.registry import (
    KNOWN_PROVIDERS,
    LOCAL_PROVIDERS,
    ProviderSpec,
    UnknownProviderError,
    build_local_inference,
    build_provider,
    provider_names,
    resolve_spec,
)
from paa.models.router import (
    EscalatingModelRouter,
    EscalationDecision,
    ProviderUsage,
    resolve_model_settings,
)

if TYPE_CHECKING:
    from paa.config import ModelSettings, Settings
    from paa.ledger.store import LedgerStore

__all__ = [
    "KNOWN_PROVIDERS",
    "LOCAL_PROVIDERS",
    "AnthropicProvider",
    "CompletionRequest",
    "CompletionResponse",
    "EchoProvider",
    "EscalatingModelRouter",
    "EscalationDecision",
    "GeminiProvider",
    "MalformedSchemaError",
    "Message",
    "ModelProvider",
    "ModelTier",
    "ModelUnavailableError",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderSpec",
    "ProviderUsage",
    "StructuredOutputError",
    "UnknownProviderError",
    "build_frontier_provider",
    "build_local_inference",
    "build_local_provider",
    "build_provider",
    "extract_json_object",
    "get_embedder",
    "get_model_router",
    "minimal_instance_for_schema",
    "provider_names",
    "redact",
    "redact_detail",
    "resolve_model_settings",
    "resolve_spec",
    "validate_against_schema",
]


def build_local_provider(settings: ModelSettings) -> ModelProvider:
    """Construct the local tier from configuration.

    Backward compatible by construction: ``ollama``/``llamacpp``/``echo`` build
    exactly as they did before the registry existed and keep reading
    :attr:`~paa.config.ModelSettings.local_base_url`. Any other registry name
    (``lmstudio``, ``vllm``, ``tgwebui`` ...) resolves through
    :func:`~paa.models.registry.build_provider` and uses that backend's
    conventional localhost port unless
    :attr:`~paa.config.ModelSettings.local_api_base` overrides it.

    ``llamacpp`` still maps to the OpenAI-compatible provider rather than a
    dedicated class: ``llama-server``, LM Studio and the rest all expose
    ``/v1/chat/completions``, so a separate implementation would be the same
    code under a different name.

    These fast paths construct their providers directly rather than through
    :func:`~paa.models.registry.build_provider`, so they thread
    :attr:`~paa.config.ModelSettings.max_transport_retries` themselves — the
    transport-retry budget is the same knob on every path (SPEC DEVIATION
    (docs/adr/0021)).
    """
    name = settings.local_provider
    if name == "echo":
        return EchoProvider(
            model=settings.local_model,
            max_retries=settings.max_retries,
            max_transport_retries=settings.max_transport_retries,
        )
    if name == "llamacpp":
        return OpenAICompatibleProvider(
            settings.local_model,
            base_url=settings.local_base_url,
            tier=ModelTier.LOCAL,
            name="llamacpp",
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            max_transport_retries=settings.max_transport_retries,
        )
    if name == "ollama":
        return OllamaProvider(
            settings.local_model,
            base_url=settings.local_base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            max_transport_retries=settings.max_transport_retries,
        )
    return build_provider(
        name,
        model=settings.local_model,
        settings=settings,
        base_url=settings.local_api_base,
    )


def build_frontier_provider(settings: ModelSettings) -> ModelProvider | None:
    """Construct the escalation tier, or ``None`` when escalation is disabled.

    Returning ``None`` for ``"none"`` is what makes a fully air-gapped
    deployment expressible in configuration rather than requiring a code path:
    the router simply has nowhere to escalate to, and says so when asked.

    The direct constructions here thread
    :attr:`~paa.config.ModelSettings.max_transport_retries` themselves, so the
    transport-retry budget does not diverge between these fast paths and the
    registry rows (SPEC DEVIATION (docs/adr/0021)).
    """
    name = settings.escalation_provider
    if name == "none":
        return None
    if name == "openai_compatible":
        if not settings.escalation_base_url:
            raise ValueError(
                "models.escalation_provider is 'openai_compatible' but "
                "models.escalation_base_url is not set"
            )
        return OpenAICompatibleProvider(
            settings.escalation_model,
            base_url=settings.escalation_base_url,
            api_key=os.environ.get(settings.escalation_api_key_env) or None,
            tier=ModelTier.FRONTIER,
            name="openai_compatible",
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            max_transport_retries=settings.max_transport_retries,
        )
    if name == "anthropic":
        return AnthropicProvider(
            settings.escalation_model,
            api_key_env=settings.escalation_api_key_env,
            base_url=settings.escalation_base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            max_transport_retries=settings.max_transport_retries,
        )
    # Any other registry platform (gemini, openai, groq, openrouter ...). The
    # platform's own default credential variable is used unless the deployment
    # overrode escalation_api_key_env away from the Anthropic default.
    api_key_env = (
        settings.escalation_api_key_env
        if settings.escalation_api_key_env != "ANTHROPIC_API_KEY"
        else None
    )
    return build_provider(
        name,
        model=settings.escalation_model,
        settings=settings,
        tier=ModelTier.FRONTIER,
        base_url=settings.escalation_api_base or settings.escalation_base_url,
        api_key_env=api_key_env,
    )


def get_model_router(
    settings: Settings | ModelSettings,
    ledger_store: LedgerStore | None = None,
) -> EscalatingModelRouter:
    """Build the configured router.

    Accepts full :class:`~paa.config.Settings` or just its ``models`` sub-model,
    because half the call sites have one and half have the other.

    Constructing a provider performs no I/O and opens no connection, so this is
    safe to call during synchronous startup wiring.

    **The whole ``settings`` is handed to the router, not just its model
    half.** The router's healthcheck probes the frontier — a network call —
    and it may only do that when the deployment's permission mode permits
    escalation. The mode lives on ``Settings.policy``, so a router built from
    the model half alone has no way to know it; it falls back to the
    most restrictive mode there is (``LOCKDOWN``), which is safe and wrong
    for a deployment that is not in LOCKDOWN. Narrowing the argument here
    is what made the safe default the *only* answer the production router
    could ever give.

    A caller that hands over only ``ModelSettings`` — which carries no mode
    at all — still gets the fail-closed default, and that is deliberate: a
    router that does not know its mode has no business assuming that
    escalation is permitted.

    :raises TypeError: if ``settings`` is neither a
        :class:`~paa.config.Settings` nor a
        :class:`~paa.config.ModelSettings`. The refusal is the point — see
        :func:`~paa.models.router.resolve_model_settings` for what the silent
        default it replaced cost a deployment.
    """
    # A full ``Settings`` keeps the model configuration one level down, under
    # ``.models``; a bare ``ModelSettings`` *is* that configuration. The
    # resolution is an ``isinstance`` test rather than a ``getattr(settings,
    # "models", settings)`` fallback, because only the former tells a type
    # checker which of the two it is holding. ``getattr`` looks equivalent and
    # is not: the ``models`` attribute exists on one member of the union and
    # not the other, so the fallback is the union itself and every attribute
    # read off the result stops being checked.
    #
    # ``paa.storage.vector.embeddings.resolve_model_settings`` is the same
    # idiom in the storage layer, and is deliberately not reused here: that
    # module imports numpy at module scope, and :mod:`paa.models.embeddings`
    # already defers its import of it into the function body. Reusing the
    # helper here would put that module-scope import back on the
    # ``import paa.models`` path, for a function that needs neither numpy nor
    # the vector stack.
    models = resolve_model_settings(settings)
    return EscalatingModelRouter(
        build_local_provider(models),
        build_frontier_provider(models),
        settings=settings,
        ledger_store=ledger_store,
    )
