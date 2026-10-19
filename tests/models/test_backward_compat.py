"""The registry must not change what the old provider names already do."""

from __future__ import annotations

import pytest

from paa.config import ModelSettings
from paa.models import build_frontier_provider, build_local_provider, get_model_router
from paa.models.anthropic_provider import AnthropicProvider
from paa.models.base import ModelTier
from paa.models.echo_provider import EchoProvider
from paa.models.gemini_provider import GeminiProvider
from paa.models.ollama_provider import OllamaProvider
from paa.models.openai_compatible_provider import OpenAICompatibleProvider


def test_defaults_are_ollama_local_and_anthropic_frontier() -> None:
    settings = ModelSettings()
    local = build_local_provider(settings)
    frontier = build_frontier_provider(settings)

    assert isinstance(local, OllamaProvider)
    assert local.name == "ollama"
    assert local.base_url == "http://127.0.0.1:11434"
    assert isinstance(frontier, AnthropicProvider)


def test_echo_local_provider_unchanged() -> None:
    local = build_local_provider(ModelSettings(local_provider="echo"))
    assert isinstance(local, EchoProvider)


def test_llamacpp_still_honours_local_base_url() -> None:
    settings = ModelSettings(local_provider="llamacpp", local_base_url="http://127.0.0.1:9999")
    local = build_local_provider(settings)
    assert isinstance(local, OpenAICompatibleProvider)
    assert local.name == "llamacpp"
    # The legacy field still wins for the legacy name.
    assert local.base_url == "http://127.0.0.1:9999"
    assert local.tier is ModelTier.LOCAL


def test_escalation_none_disables_frontier() -> None:
    assert build_frontier_provider(ModelSettings(escalation_provider="none")) is None


def test_openai_compatible_escalation_still_requires_base_url() -> None:
    with pytest.raises(ValueError, match="escalation_base_url"):
        build_frontier_provider(ModelSettings(escalation_provider="openai_compatible"))


def test_get_model_router_wires_local_and_frontier() -> None:
    router = get_model_router(ModelSettings())
    assert isinstance(router.local, OllamaProvider)
    assert isinstance(router.frontier, AnthropicProvider)
    assert router.can_escalate is True


# -- new capability, exercised through the same config surface ---------------


def test_lmstudio_selects_openai_provider_at_its_own_port() -> None:
    local = build_local_provider(ModelSettings(local_provider="lmstudio"))
    assert isinstance(local, OpenAICompatibleProvider)
    assert local.name == "lmstudio"
    assert local.base_url == "http://localhost:1234"
    assert local.tier is ModelTier.LOCAL


def test_local_api_base_overrides_a_registry_provider() -> None:
    settings = ModelSettings(local_provider="vllm", local_api_base="http://gpu-box:8000")
    local = build_local_provider(settings)
    assert local.base_url == "http://gpu-box:8000"


def test_gemini_escalation_reads_its_own_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    frontier = build_frontier_provider(
        ModelSettings(escalation_provider="gemini", escalation_model="gemini-2.0-flash")
    )
    assert isinstance(frontier, GeminiProvider)
    # The Anthropic default was left untouched, so the Gemini spec's own
    # credential variables are used rather than ANTHROPIC_API_KEY.
    assert frontier.api_key_envs == ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def test_groq_escalation_is_frontier_tier() -> None:
    frontier = build_frontier_provider(ModelSettings(escalation_provider="groq"))
    assert isinstance(frontier, OpenAICompatibleProvider)
    assert frontier.name == "groq"
    assert frontier.tier is ModelTier.FRONTIER


# -- ADR-0021: one transport-retry knob, on every construction path ---------


def test_the_local_fast_paths_thread_the_transport_retry_budget() -> None:
    """``build_local_provider`` builds some providers itself, so it must thread
    the setting itself.

    ``echo``/``llamacpp``/``ollama`` are constructed directly rather than
    through the registry, so nothing else does it for them. Dropping the
    kwarg there leaves the provider on its own default and the knob silently
    does nothing on the path most deployments use — which is exactly the
    "every construction path honours one knob" claim (SPEC DEVIATION
    (docs/adr/0021)).
    """
    settings = ModelSettings(local_provider="ollama", max_transport_retries=5)

    local = build_local_provider(settings)
    assert isinstance(local, OllamaProvider)
    assert local.max_transport_retries == 5

    llamacpp = build_local_provider(
        ModelSettings(local_provider="llamacpp", max_transport_retries=5)
    )
    assert isinstance(llamacpp, OpenAICompatibleProvider)
    assert llamacpp.max_transport_retries == 5


def test_the_frontier_fast_paths_thread_the_transport_retry_budget() -> None:
    """The same knob, on the escalation side of the same claim.

    ``openai_compatible`` and ``anthropic`` are constructed directly here
    too, so the frontier tier has exactly as much of a hole in it as the
    local one had.
    """
    frontier = build_frontier_provider(
        ModelSettings(
            escalation_provider="openai_compatible",
            escalation_base_url="https://gateway.test.invalid",
            max_transport_retries=5,
        )
    )
    assert isinstance(frontier, OpenAICompatibleProvider)
    assert frontier.max_transport_retries == 5

    anthropic = build_frontier_provider(
        ModelSettings(escalation_provider="anthropic", max_transport_retries=5)
    )
    assert isinstance(anthropic, AnthropicProvider)
    assert anthropic.max_transport_retries == 5
