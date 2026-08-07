"""Shared helpers for the model-provider tests.

Every test in this package is network-free: providers are handed an
``httpx.AsyncClient`` backed by :class:`httpx.MockTransport`, so a request is
shaped and asserted on without a socket ever opening. The one cross-cutting
safety property — an API key must never reach a log line, a URL or an exception
— gets its own reusable assertion here so every test can spend one line on it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

#: A credential-shaped token. Long enough to trip the generic redactor in
#: :func:`paa.models.base.redact`, and distinctive enough to grep for.
FAKE_KEY = "sk-test-abcdef0123456789ABCDEF"


class RequestRecorder:
    """A :class:`httpx.MockTransport` handler that records what it was sent.

    Captures the last request (and its decoded JSON body) so a test can assert
    on the URL, headers and payload the provider produced, then returns a canned
    response built by ``responder``.
    """

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self._responder = responder
        self.request: httpx.Request | None = None
        self.body: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        raw = request.content
        self.body = json.loads(raw) if raw else {}
        return self._responder(request)

    @property
    def url(self) -> str:
        assert self.request is not None, "no request was captured"
        return str(self.request.url)

    def header(self, name: str) -> str | None:
        assert self.request is not None, "no request was captured"
        return self.request.headers.get(name)


def mock_client(recorder: RequestRecorder) -> httpx.AsyncClient:
    """An async client whose every request is served by ``recorder``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


def openai_chat_response(content: str, *, model: str = "test-model") -> httpx.Response:
    """A minimal, valid ``/v1/chat/completions`` body."""
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
    )


def gemini_response(text: str, *, model: str = "gemini-test") -> httpx.Response:
    """A minimal, valid ``generateContent`` body."""
    return httpx.Response(
        200,
        json={
            "modelVersion": model,
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 5,
                "totalTokenCount": 8,
            },
        },
    )


def assert_no_key_leak(*blobs: object, key: str = FAKE_KEY) -> None:
    """Fail if ``key`` (or a raw ``Bearer <key>``) appears in any blob.

    The single most important assertion in this suite: it is run against
    exception text, captured log events and request URLs alike.
    """
    for blob in blobs:
        text = str(blob)
        assert key not in text, f"API key leaked into: {text!r}"
        assert "Bearer sk-test" not in text, f"bearer credential leaked into: {text!r}"
