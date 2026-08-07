"""The registry maps names to the right provider class, honestly and loudly."""

from __future__ import annotations

import pytest

from paa.models.anthropic_provider import AnthropicProvider
from paa.models.base import ModelTier
from paa.models.echo_provider import EchoProvider
from paa.models.gemini_provider import GeminiProvider
from paa.models.ollama_provider import OllamaProvider
from paa.models.openai_compatible_provider import OpenAICompatibleProvider
from paa.models.registry import (
    LOCAL_PROVIDERS,
    UnknownProviderError,
    build_local_inference,
    build_provider,
    provider_names,
    resolve_spec,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("openai", OpenAICompatibleProvider),
        ("groq", OpenAICompatibleProvider),
        ("openrouter", OpenAICompatibleProvider),
        ("together", OpenAICompatibleProvider),
        ("deepseek", OpenAICompatibleProvider),
        ("xai", OpenAICompatibleProvider),
        ("mistral", OpenAICompatibleProvider),
        ("fireworks", OpenAICompatibleProvider),
        ("perplexity", OpenAICompatibleProvider),
        ("azure", OpenAICompatibleProvider),
        ("lmstudio", OpenAICompatibleProvider),
        ("llamacpp", OpenAICompatibleProvider),
        ("vllm", OpenAICompatibleProvider),
        ("tgwebui", OpenAICompatibleProvider),
        ("ollama", OllamaProvider),
        ("anthropic", AnthropicProvider),
        ("gemini", GeminiProvider),
        ("echo", EchoProvider),
    ],
)
def test_build_provider_returns_the_right_class(name: str, expected: type) -> None:
    assert isinstance(build_provider(name), expected)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("grok", "xai"),
        ("google", "gemini"),
        ("lm-studio", "lmstudio"),
        ("llama-server", "llamacpp"),
    ],
)
def test_aliases_resolve_to_canonical_spec(alias: str, canonical: str) -> None:
    assert resolve_spec(alias).name == canonical


def test_unknown_provider_raises_a_clear_listing_error() -> None:
    with pytest.raises(UnknownProviderError) as excinfo:
        build_provider("not-a-real-provider")
    message = str(excinfo.value)
    assert "not-a-real-provider" in message
    # The actionable part: the caller is told what *is* valid.
    assert "gemini" in message and "ollama" in message


def test_local_providers_are_tier_local() -> None:
    for name in LOCAL_PROVIDERS:
        assert build_provider(name).tier is ModelTier.LOCAL


def test_hosted_providers_are_tier_frontier() -> None:
    for name in ("openai", "groq", "anthropic", "gemini", "mistral"):
        assert build_provider(name).tier is ModelTier.FRONTIER


def test_build_local_inference_defaults_to_loopback() -> None:
    provider = build_local_inference("lmstudio")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "http://localhost:1234"
    assert provider.tier is ModelTier.LOCAL


def test_build_local_inference_rejects_a_hosted_backend() -> None:
    with pytest.raises(UnknownProviderError):
        build_local_inference("openai")


def test_provider_names_are_sorted_and_include_the_platforms() -> None:
    names = provider_names()
    assert names == sorted(names)
    for expected in ("openai", "groq", "gemini", "anthropic", "ollama", "vllm"):
        assert expected in names


def test_model_override_is_honoured() -> None:
    provider = build_provider("openai", model="gpt-4o")
    assert provider.model == "gpt-4o"


def test_azure_pins_the_deployment_in_the_path() -> None:
    provider = build_provider("azure", model="my-deploy", base_url="https://r.openai.azure.com")
    # The deployment name is baked into the chat path; api-version rides along.
    assert "/openai/deployments/my-deploy/chat/completions" in provider._chat_path
    assert provider._extra_query is not None and "api-version=" in provider._extra_query
    assert provider._auth_header == "api-key"
