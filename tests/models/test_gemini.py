"""Gemini speaks its own wire format — this pins the native shape both ways."""

from __future__ import annotations

import json

import httpx
import pytest
import structlog

from paa.models.base import CompletionRequest, Message, ModelUnavailableError
from paa.models.gemini_provider import GeminiProvider

from .conftest import FAKE_KEY, RequestRecorder, assert_no_key_leak, gemini_response, mock_client

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


async def test_request_uses_native_contents_and_system_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    recorder = RequestRecorder(lambda _req: gemini_response("hi there"))
    provider = GeminiProvider(client=mock_client(recorder))

    request = CompletionRequest(
        messages=(
            Message.system("You are terse."),
            Message.user("hello"),
            Message.assistant("hi"),
            Message.user("again"),
        )
    )
    response = await provider.complete(request)

    assert response.text == "hi there"
    assert response.provider == "gemini"
    assert response.prompt_tokens == 3 and response.completion_tokens == 5

    # Native path, native model-in-URL.
    assert recorder.request is not None
    assert recorder.request.url.path == "/v1beta/models/gemini-2.0-flash:generateContent"

    # System prompt is lifted out; assistant becomes "model".
    assert recorder.body["systemInstruction"] == {"parts": [{"text": "You are terse."}]}
    roles = [c["role"] for c in recorder.body["contents"]]
    assert roles == ["user", "model", "user"]
    assert recorder.body["generationConfig"]["temperature"] == 0.0
    assert recorder.body["generationConfig"]["maxOutputTokens"] == 1024


async def test_auth_is_a_header_never_the_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    recorder = RequestRecorder(lambda _req: gemini_response("ok"))
    provider = GeminiProvider(client=mock_client(recorder))
    await provider.complete(CompletionRequest(messages=(Message.user("x"),)))

    assert recorder.header("x-goog-api-key") == FAKE_KEY
    # The credential must not ride in the URL where _safe_url could not scrub it.
    assert_no_key_leak(recorder.url)


async def test_google_api_key_is_the_fallback_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", FAKE_KEY)
    recorder = RequestRecorder(lambda _req: gemini_response("ok"))
    provider = GeminiProvider(client=mock_client(recorder))
    await provider.complete(CompletionRequest(messages=(Message.user("x"),)))
    assert recorder.header("x-goog-api-key") == FAKE_KEY


async def test_structured_output_sets_response_schema_and_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    recorder = RequestRecorder(lambda _req: gemini_response(json.dumps({"answer": "42"})))
    provider = GeminiProvider(client=mock_client(recorder))

    request = CompletionRequest(messages=(Message.user("six times seven?"),))
    result = await provider.complete_structured(request, _SCHEMA)

    assert result == {"answer": "42"}
    config = recorder.body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    # The schema is projected to Gemini's subset: type upper-cased, unsupported
    # `additionalProperties` dropped.
    schema = config["responseSchema"]
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["answer"]["type"] == "STRING"
    assert "additionalProperties" not in schema


async def test_missing_credentials_raise_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    provider = GeminiProvider()

    assert await provider.healthcheck() is False
    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(CompletionRequest(messages=(Message.user("x"),)))
    # The variable names are actionable and safe; no value could leak here.
    assert "GEMINI_API_KEY" in str(excinfo.value)


async def test_error_body_echoing_the_key_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    recorder = RequestRecorder(
        lambda _req: httpx.Response(403, text=f"denied for key {FAKE_KEY}")
    )
    provider = GeminiProvider(client=mock_client(recorder))

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(ModelUnavailableError) as excinfo,
    ):
        await provider.complete(CompletionRequest(messages=(Message.user("x"),)))

    assert_no_key_leak(str(excinfo.value), excinfo.value.details, *logs)
