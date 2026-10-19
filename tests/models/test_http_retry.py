"""Transport retry: transient failures are re-asked, deterministic ones are not.

One POST used to be the whole story — ``models/_http.py`` issued a single
request and *any* hiccup (429, 5xx, timeout, connection reset) surfaced as
``ModelUnavailableError``, which ends ``paa agent`` outright when escalation is
policy-blocked. The structured-output ``max_retries`` loop cannot help there:
there is no answer to repair when the transport itself failed.

These tests pin the retryable class from ADR-0021 against real request bytes:
408/429/5xx and timeout/connection errors re-ask within the budget, other 4xx
and malformed bodies fail on the first response, the ``model.http_retry`` log
line appears without a response body in it, and the redaction guarantee
survives retries unchanged. Providers are built with a ~0 backoff base so the
whole module runs in milliseconds.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
import structlog

from paa.config import ModelSettings
from paa.models.base import CompletionRequest, Message, ModelUnavailableError
from paa.models.ollama_provider import OllamaProvider
from paa.models.openai_compatible_provider import OpenAICompatibleProvider
from paa.models.registry import build_provider

from .conftest import (
    FAKE_KEY,
    RequestRecorder,
    assert_no_key_leak,
    mock_client,
    openai_chat_response,
)

Responder = Callable[[httpx.Request], httpx.Response]

_REQUEST = CompletionRequest(messages=(Message.user("ping"),))


class CountingRecorder(RequestRecorder):
    """A :class:`RequestRecorder` that counts every request it saw.

    ``RequestRecorder`` keeps only the *last* request; transport retry is
    about how many were sent, which is the assertion this suite leans on most.
    """

    def __init__(self, responder: Responder) -> None:
        super().__init__(responder)
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        return super().__call__(request)


def scripted(*steps: Responder) -> Responder:
    """A responder that follows ``steps`` in order, repeating the last one.

    Steps may raise: that is how a transport exception is simulated through a
    ``MockTransport``, and it propagates to the client exactly as a socket
    failure would — httpx does not wrap handler exceptions.
    """
    seen = 0

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        step = steps[min(seen, len(steps) - 1)]
        seen += 1
        return step(request)

    return responder


def always(response: httpx.Response) -> Responder:
    """A responder that returns ``response`` forever."""
    return lambda _req: response


def provider_for(
    recorder: CountingRecorder,
    *,
    max_transport_retries: int = 2,
    timeout: float = 10.0,
    retry_backoff_base: float = 0.0,
    retry_backoff_cap: float = 0.0,
) -> OpenAICompatibleProvider:
    """An OpenAI-shaped provider wired to ``recorder`` with retries on.

    Backoff defaults to ~0 so retry assertions do not sleep; a test that needs
    the *schedule* to matter passes a real base (see the Retry-After case,
    where a header override must beat a 5-second default).
    """
    return OpenAICompatibleProvider(
        "test-model",
        base_url="https://api.test.invalid",
        api_key=FAKE_KEY,
        timeout=timeout,
        max_transport_retries=max_transport_retries,
        retry_backoff_base=retry_backoff_base,
        retry_backoff_cap=retry_backoff_cap,
        client=mock_client(recorder),
    )


def retry_events(logs: list[structlog.typing.EventDict]) -> list[structlog.typing.EventDict]:
    """Only the ``model.http_retry`` entries from a captured log stream."""
    return [entry for entry in logs if entry["event"] == "model.http_retry"]


async def test_flaky_500_recovers_on_second_attempt() -> None:
    """One transient 500 costs a second request, not the run."""
    recorder = CountingRecorder(
        scripted(always(httpx.Response(500, text="flap")), lambda _r: openai_chat_response("pong"))
    )

    provider = provider_for(recorder)
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)

    assert result.text == "pong"
    assert recorder.count == 2

    retries = retry_events(logs)
    assert len(retries) == 1
    assert retries[0]["provider"] == provider.name
    assert retries[0]["attempt"] == 1
    assert retries[0]["status_code"] == 500
    assert retries[0]["delay_s"] == 0.0
    assert "https://api.test.invalid" in retries[0]["url"]


async def test_persistent_500_exhausts_the_transport_budget() -> None:
    """The last error is today's error: same message, same ``status_code``."""
    recorder = CountingRecorder(always(httpx.Response(500, text="still down")))

    provider = provider_for(recorder, max_transport_retries=2)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(ModelUnavailableError) as excinfo,
    ):
        await provider.complete(_REQUEST)

    assert recorder.count == 3  # max_transport_retries + 1
    assert excinfo.value.message == "model endpoint returned HTTP 500"
    assert excinfo.value.details["status_code"] == 500

    # The budget is attempts, not sleeps: two retries were logged and warned
    # about before the third failure surfaced.
    assert len(retry_events(logs)) == 2


async def test_http_400_is_never_retried() -> None:
    """400/401/403 are deterministic — replaying them is just slower."""
    recorder = CountingRecorder(always(httpx.Response(400, text="malformed request")))

    provider = provider_for(recorder)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(ModelUnavailableError) as excinfo,
    ):
        await provider.complete(_REQUEST)

    assert recorder.count == 1
    assert excinfo.value.details["status_code"] == 400
    assert retry_events(logs) == []


async def test_429_honours_a_numeric_retry_after_header() -> None:
    """A server-specified wait overrides the backoff schedule.

    The backoff base is deliberately *worse* than the header here (5 s vs
    ``Retry-After: 0``): if the header were ignored, the logged delay would be
    5.0 and — with a 1 s timeout — the deadline check would raise instead of
    retrying, so the regression fails fast rather than sleeping.
    """
    recorder = CountingRecorder(
        scripted(
            always(httpx.Response(429, text="slow down", headers={"Retry-After": "0"})),
            lambda _r: openai_chat_response("pong"),
        )
    )

    provider = provider_for(
        recorder, timeout=1.0, retry_backoff_base=5.0, retry_backoff_cap=5.0
    )
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)

    assert result.text == "pong"
    assert recorder.count == 2

    retries = retry_events(logs)
    assert len(retries) == 1
    assert retries[0]["status_code"] == 429
    assert retries[0]["delay_s"] == 0.0  # from Retry-After, not 2**attempt


async def test_timeout_is_retried_until_it_succeeds() -> None:
    """Two read timeouts then a good answer: the third attempt wins."""
    def boom(_req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated read timeout")

    recorder = CountingRecorder(scripted(boom, boom, lambda _r: openai_chat_response("pong")))

    provider = provider_for(recorder, max_transport_retries=2)
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)

    assert result.text == "pong"
    assert recorder.count == 3
    retries = retry_events(logs)
    assert len(retries) == 2
    assert retries[0]["status_code"] is None  # transport failure: no HTTP status


async def test_connect_error_translates_after_retries_are_exhausted() -> None:
    """Connection failures still collapse to the familiar reach-error shape."""
    def refused(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    # ``refused`` is already an always-failing responder — it raises on every
    # attempt — so it goes in directly. ``always`` takes a *response* to hand
    # back, and would hand back the function itself instead of calling it.
    recorder = CountingRecorder(refused)

    provider = provider_for(recorder, max_transport_retries=1)
    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(_REQUEST)

    assert recorder.count == 2
    assert excinfo.value.message == "could not reach the model endpoint: ConnectError"
    assert "ConnectError" in str(excinfo.value)


async def test_retry_never_leaks_a_key_from_the_response_body() -> None:
    """A 500 body echoing the key must not reach logs, the error or details."""
    recorder = CountingRecorder(
        always(httpx.Response(500, text=f"upstream rejected key {FAKE_KEY}"))
    )

    provider = provider_for(recorder, max_transport_retries=1)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(ModelUnavailableError) as excinfo,
    ):
        await provider.complete(_REQUEST)

    assert recorder.count == 2  # it did retry, so the body *was* handled twice
    assert_no_key_leak(str(excinfo.value), excinfo.value.details, *logs)
    # The retry log line carries the status but never the body at all.
    retries = retry_events(logs)
    assert len(retries) == 1
    assert set(retries[0]) == {
        "event",
        "log_level",
        "provider",
        "attempt",
        "status_code",
        "delay_s",
        "url",
    }


async def test_non_json_body_is_not_retried() -> None:
    """A 200 with HTML is an answer, not a transport event."""
    recorder = CountingRecorder(always(httpx.Response(200, text="<html>gateway page</html>")))

    provider = provider_for(recorder)
    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(_REQUEST)

    assert recorder.count == 1
    assert excinfo.value.message == "model endpoint returned a non-JSON body"


def test_registry_threads_max_transport_retries_from_settings() -> None:
    """Settings reach the provider the same way ``max_retries`` does."""
    provider = build_provider(
        "ollama",
        settings=ModelSettings(max_transport_retries=5),
        client=mock_client(CountingRecorder(always(openai_chat_response("x")))),
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.max_transport_retries == 5

    defaulted = build_provider(
        "ollama",
        client=mock_client(CountingRecorder(always(openai_chat_response("x")))),
    )
    assert isinstance(defaulted, OllamaProvider)
    assert defaulted.max_transport_retries == 2
