"""Every OpenAI-compatible platform: right URL, right auth, structured round-trip.

One parametrised body covers OpenAI, Groq, OpenRouter, Together, DeepSeek, xAI,
Mistral, Fireworks, Perplexity and Azure. The point of the sweep is that each
platform's *quirk* — base URL, path prefix, auth header shape, Azure's
``api-version`` — is exercised against the real request bytes, not asserted in
the abstract.
"""

from __future__ import annotations

import json

import httpx
import pytest
import structlog

from paa.models.base import CompletionRequest, Message
from paa.models.registry import build_provider, resolve_spec

from .conftest import (
    FAKE_KEY,
    RequestRecorder,
    assert_no_key_leak,
    mock_client,
    openai_chat_response,
)

# (registry name, base_url override, model override). Azure needs both because
# the resource endpoint and the deployment name are deployment-specific.
HOSTED_PLATFORMS = [
    ("openai", None, None),
    ("groq", None, None),
    ("openrouter", None, None),
    ("together", None, None),
    ("deepseek", None, None),
    ("xai", None, None),
    ("mistral", None, None),
    ("fireworks", None, None),
    ("perplexity", None, None),
    ("azure", "https://res.openai.azure.com", "my-deploy"),
]

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _expected(name: str, base_override: str | None, model_override: str | None) -> tuple[str, str]:
    """Return the (host, path) the provider is expected to POST to."""
    spec = resolve_spec(name)
    model = model_override or spec.default_model
    base = base_override or spec.default_base_url
    assert base is not None
    chat_path = (
        spec.chat_path_template.format(model=model)
        if spec.chat_path_template
        else spec.chat_path
    )
    base_url = httpx.URL(base)
    return base_url.host, base_url.path.rstrip("/") + chat_path


@pytest.mark.parametrize(("name", "base_override", "model_override"), HOSTED_PLATFORMS)
async def test_platform_request_shape_and_structured_roundtrip(
    name: str,
    base_override: str | None,
    model_override: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = resolve_spec(name)
    monkeypatch.setenv(spec.api_key_env[0], FAKE_KEY)

    recorder = RequestRecorder(lambda _req: openai_chat_response(json.dumps({"answer": "42"})))
    provider = build_provider(
        name,
        model=model_override,
        base_url=base_override,
        client=mock_client(recorder),
    )

    request = CompletionRequest(messages=(Message.user("What is six times seven?"),))

    with structlog.testing.capture_logs() as logs:
        result = await provider.complete_structured(request, _SCHEMA)

    # Structured output round-trips through the generic validate-and-return path.
    assert result == {"answer": "42"}

    # Went to the right endpoint.
    host, path = _expected(name, base_override, model_override)
    assert recorder.request is not None
    assert recorder.request.url.host == host
    assert recorder.request.url.path == path

    # Auth header carries the right shape for the platform.
    if spec.auth_header == "api-key":  # Azure
        assert recorder.header("api-key") == FAKE_KEY
        assert recorder.header("authorization") is None
        assert b"api-version=" in recorder.request.url.query
    else:
        assert recorder.header("authorization") == f"Bearer {FAKE_KEY}"

    # Body asked for constrained JSON.
    assert recorder.body["response_format"]["type"] == "json_schema"

    # The credential never appears in the URL, and never in the logs.
    assert_no_key_leak(recorder.url, *logs)


@pytest.mark.parametrize(("name", "base_override", "model_override"), HOSTED_PLATFORMS)
async def test_platform_error_body_is_redacted(
    name: str,
    base_override: str | None,
    model_override: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that echoes the key back in a 400 must not leak it onward."""
    from paa.models.base import ModelUnavailableError

    spec = resolve_spec(name)
    monkeypatch.setenv(spec.api_key_env[0], FAKE_KEY)

    def echo_key_400(_req: httpx.Request) -> httpx.Response:
        # Some gateways quote the offending Authorization header back verbatim.
        return httpx.Response(400, text=f"invalid request with key {FAKE_KEY}")

    recorder = RequestRecorder(echo_key_400)
    provider = build_provider(
        name,
        model=model_override,
        base_url=base_override,
        client=mock_client(recorder),
    )
    request = CompletionRequest(messages=(Message.user("hi"),))

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(ModelUnavailableError) as excinfo,
    ):
        await provider.complete(request)

    assert_no_key_leak(str(excinfo.value), excinfo.value.details, *logs)


async def test_perplexity_uses_the_no_v1_chat_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Perplexity's quirk: /chat/completions with no /v1 prefix."""
    monkeypatch.setenv("PERPLEXITY_API_KEY", FAKE_KEY)
    recorder = RequestRecorder(lambda _req: openai_chat_response("pong"))
    provider = build_provider("perplexity", client=mock_client(recorder))
    await provider.complete(CompletionRequest(messages=(Message.user("ping"),)))
    assert recorder.request is not None
    assert recorder.request.url.path == "/chat/completions"


async def test_unauthenticated_local_provider_sends_no_auth_header() -> None:
    """A localhost server needs no key, so no Authorization header is attached."""
    recorder = RequestRecorder(lambda _req: openai_chat_response("local pong"))
    provider = build_provider("vllm", client=mock_client(recorder))
    await provider.complete(CompletionRequest(messages=(Message.user("ping"),)))
    assert recorder.header("authorization") is None
