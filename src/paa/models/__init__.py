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
    Message,
    ModelProvider,
    ModelTier,
    ModelUnavailableError,
    StructuredOutputError,
    extract_json_object,
    minimal_instance_for_schema,
    redact,
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
from paa.models.router import EscalatingModelRouter, EscalationDecision, ProviderUsage

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
    """
    name = settings.local_provider
    if name == "echo":
        return EchoProvider(model=settings.local_model, max_retries=settings.max_retries)
    if name == "llamacpp":
        return OpenAICompatibleProvider(
            settings.local_model,
            base_url=settings.local_base_url,
            tier=ModelTier.LOCAL,
            name="llamacpp",
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )
    if name == "ollama":
        return OllamaProvider(
            settings.local_model,
            base_url=settings.local_base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
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
        )
    if name == "anthropic":
        return AnthropicProvider(
            settings.escalation_model,
            api_key_env=settings.escalation_api_key_env,
            base_url=settings.escalation_base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
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
    """
    models: ModelSettings = getattr(settings, "models", settings)  # type: ignore[assignment]
    return EscalatingModelRouter(
        build_local_provider(models),
        build_frontier_provider(models),
        settings=models,
        ledger_store=ledger_store,
    )
