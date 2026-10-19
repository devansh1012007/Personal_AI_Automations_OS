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
survives retries unchanged. The *budget* is pinned as firmly as the class: a
timeout that consumes its attempt is still retried, the wall-clock bound
stops the loop before the attempt count does, and both legal forms of
``Retry-After`` are honoured. Providers are built with a ~0 backoff base so
the whole module runs in milliseconds.

Two details of that budget are load-bearing and are pinned *individually*,
because the two guards that enforce it cover for each other: the loop must
not sleep into a retry its budget cannot buy, and it must not spend a
request whose budget is already gone. And the failure a timeout produces is
pinned as a *timeout*, not as an unreachable endpoint — the two say different
things to whoever is reading the ledger, and ``httpx`` makes the order of
the two ``except`` clauses the difference between them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import structlog

from paa.config import ModelSettings
from paa.models._http import _RETRY_AFTER_CAP_S, _retry_after_seconds
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


def http_date_ahead(seconds: float) -> str:
    """An HTTP-date at least ``seconds`` in the future, in the wire format.

    HTTP-date has one-second resolution, so the value is rounded *up*: a
    date rounded down could already be in the past, which would turn "the
    header was honoured" into a coin flip on the sub-second.
    """
    target = datetime.now(UTC) + timedelta(seconds=seconds)
    return format_datetime(target.replace(microsecond=0) + timedelta(seconds=1), usegmt=True)


def http_date_behind(seconds: float) -> str:
    """An HTTP-date ``seconds`` in the past, in the wire format."""
    target = datetime.now(UTC) - timedelta(seconds=seconds)
    return format_datetime(target.replace(microsecond=0), usegmt=True)


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


async def test_a_timeout_that_consumes_its_attempt_is_still_retried() -> None:
    """A read timeout that burns the per-attempt budget is still retried.

    The regression this pins: the loop's deadline used to be the *per-attempt*
    timeout, so an attempt that consumed that timeout left no room for the
    retry the knob exists to buy — the call failed after a single attempt
    even though ``max_transport_retries`` said otherwise, and "timeouts" was
    advertised as retryable while being unreachable. The handler below
    deliberately overruns the per-attempt timeout, which is what a real read
    timeout does.
    """
    timeout = 0.1
    burn = 0.12
    attempts = 3  # max_transport_retries + 1

    def slow_timeout(_req: httpx.Request) -> httpx.Response:
        time.sleep(burn)
        raise httpx.ReadTimeout("simulated read timeout")

    recorder = CountingRecorder(
        scripted(slow_timeout, slow_timeout, lambda _r: openai_chat_response("pong"))
    )

    provider = provider_for(recorder, max_transport_retries=2, timeout=timeout)
    started = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)
    elapsed = time.monotonic() - started

    assert result.text == "pong"
    assert recorder.count == attempts  # the timeout was retried, twice
    retries = retry_events(logs)
    assert len(retries) == 2
    assert retries[0]["status_code"] is None  # transport failure: no HTTP status

    # The mock transport does not enforce httpx's timeout, so the handler's
    # overrun is real wall clock: the nominal budget is ``timeout * attempts``
    # of attempt time, and the overrun is spent on top of it.
    assert elapsed >= burn
    assert elapsed < timeout * attempts + burn * attempts


async def test_the_budget_stops_the_loop_before_the_attempt_count_does() -> None:
    """The wall-clock bound is a hard bound, not a hint.

    Every attempt here overruns the per-attempt timeout, so the attempts
    cannot all fit inside the loop's budget: the loop must stop when the
    budget runs out — not when the attempt count does — and it must surface
    the fully-shaped error rather than a bare timeout.
    """
    timeout = 0.05
    burn = 0.12
    attempts = 4  # max_transport_retries + 1
    budget = timeout * attempts

    def slow_timeout(_req: httpx.Request) -> httpx.Response:
        time.sleep(burn)
        raise httpx.ReadTimeout("simulated read timeout")

    recorder = CountingRecorder(slow_timeout)

    provider = provider_for(recorder, max_transport_retries=3, timeout=timeout)
    started = time.monotonic()
    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(_REQUEST)
    elapsed = time.monotonic() - started

    # Fewer requests than the knob allows, and no more wall clock than the
    # budget plus the one attempt that overran it.
    assert recorder.count < attempts
    assert elapsed < budget + burn + 0.1

    # The error is the familiar one, in full: a truncated one would leave the
    # router with nothing to log.
    assert excinfo.value.message == "request to the model endpoint timed out"
    assert excinfo.value.details["provider"] == provider.name
    assert excinfo.value.details["url"] == "https://api.test.invalid/v1/chat/completions"
    assert excinfo.value.details["timeout_seconds"] == timeout
    assert isinstance(excinfo.value.__cause__, httpx.ReadTimeout)


async def test_retry_after_http_date_is_honoured() -> None:
    """The other legal ``Retry-After`` form: an HTTP-date.

    RFC 9110 allows delta-seconds *or* an HTTP-date, and the date is the
    form a rate-limiting gateway actually sends. Honouring it costs a real
    wait, which is the point: the alternative was a legal header the runtime
    silently dropped in favour of the exponential schedule. The backoff base
    is deliberately worse than the header here, so a runtime that ignored the
    date would log a 5-second delay instead.
    """
    recorder = CountingRecorder(
        scripted(
            always(
                httpx.Response(
                    429, text="slow down", headers={"Retry-After": http_date_ahead(0.25)}
                )
            ),
            lambda _r: openai_chat_response("pong"),
        )
    )

    provider = provider_for(
        recorder, timeout=5.0, retry_backoff_base=5.0, retry_backoff_cap=5.0
    )
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)

    assert result.text == "pong"
    assert recorder.count == 2

    retries = retry_events(logs)
    assert len(retries) == 1
    assert retries[0]["status_code"] == 429
    # The date resolved against the wall clock: positive, and no longer than
    # the rounding of the HTTP-date format allows.
    assert 0.0 < retries[0]["delay_s"] <= 1.5


async def test_retry_after_http_date_in_the_past_means_no_wait() -> None:
    """A date already past is a retry that is due: wait zero, never negative."""
    recorder = CountingRecorder(
        scripted(
            always(
                httpx.Response(
                    429, text="slow down", headers={"Retry-After": http_date_behind(30)}
                )
            ),
            lambda _r: openai_chat_response("pong"),
        )
    )

    provider = provider_for(
        recorder, timeout=5.0, retry_backoff_base=5.0, retry_backoff_cap=5.0
    )
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)

    assert result.text == "pong"
    assert recorder.count == 2

    retries = retry_events(logs)
    assert len(retries) == 1
    assert retries[0]["delay_s"] == 0.0  # the header, not the 5-second schedule


async def test_unreadable_retry_after_falls_back_to_the_schedule() -> None:
    """A header we cannot read is not a header we sleep on."""
    recorder = CountingRecorder(
        scripted(
            always(
                httpx.Response(
                    429, text="slow down", headers={"Retry-After": "not-a-date"}
                )
            ),
            lambda _r: openai_chat_response("pong"),
        )
    )

    provider = provider_for(
        recorder, timeout=5.0, retry_backoff_base=0.2, retry_backoff_cap=5.0
    )
    with structlog.testing.capture_logs() as logs:
        result = await provider.complete(_REQUEST)

    assert result.text == "pong"
    assert recorder.count == 2

    retries = retry_events(logs)
    assert len(retries) == 1
    assert retries[0]["delay_s"] == 0.2  # the schedule, not the header


def test_retry_after_is_capped_in_both_forms() -> None:
    """The header is a suggestion; this runtime's cap wins, in both forms.

    Checked against the helper directly: honouring a far-future date means
    *sleeping* for the cap, and a test that sleeps ten seconds to prove a
    clamp is a bad trade.
    """
    numeric = httpx.Response(429, headers={"Retry-After": "3600"})
    dated = httpx.Response(429, headers={"Retry-After": http_date_ahead(3600)})

    assert _retry_after_seconds(numeric) == _RETRY_AFTER_CAP_S
    assert _retry_after_seconds(dated) == _RETRY_AFTER_CAP_S


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


@pytest.mark.parametrize(
    "exc_type",
    [httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout],
    ids=["timeout", "read-timeout", "connect-timeout"],
)
async def test_a_timeout_is_reported_as_a_timeout_not_a_reach_failure(
    exc_type: type[httpx.TimeoutException],
) -> None:
    """A timeout and an unreachable endpoint are different diagnoses.

    On httpx 0.28.1 ``TimeoutException`` is a subclass of ``HTTPError``, so
    the order of the two ``except`` clauses in ``models/_http.py`` decides
    which message the operator sees. With ``HTTPError`` first, a read timeout
    is reported as ``"could not reach the model endpoint: ReadTimeout"`` — the
    endpoint is blamed for being down when it merely did not answer in time,
    and the two failures stop being distinguishable in the ledger.

    The whole timeout class is asserted, not one member of it: the handler
    catches ``TimeoutException``, so a member that escaped it would be
    reported as a reach failure.
    """

    def boom(_req: httpx.Request) -> httpx.Response:
        raise exc_type("simulated timeout")

    recorder = CountingRecorder(boom)

    # One attempt: the assertion is about the message, and a single attempt
    # keeps it to that.
    provider = provider_for(recorder, max_transport_retries=0)
    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(_REQUEST)

    assert excinfo.value.message == "request to the model endpoint timed out"
    assert "could not reach the model endpoint" not in excinfo.value.message
    # The reach-failure shape carries no timeout budget; this one does.
    assert excinfo.value.details["timeout_seconds"] == 10.0
    assert isinstance(excinfo.value.__cause__, httpx.TimeoutException)


async def test_the_loop_does_not_sleep_past_its_budget() -> None:
    """The budget is a hard bound on wall clock, not a hint.

    The retry the budget cannot buy must not be *waited for*: the loop has to
    surface the failure it already has, at the moment the budget runs out,
    rather than sit in a backoff that cannot be followed by the attempt it
    was buying. The backoff here is five seconds against a budget of 0.15 s,
    so the difference between raising and sleeping is unmistakable.

    The wall clock is the discriminator, and it has to be: with the guard
    gone, the loop sleeps past its deadline and the *next* guard stops it
    with the very same error — only later. A test that only checked the
    message would see no difference at all.
    """
    timeout = 0.05
    max_transport_retries = 2  # a budget of 0.15 s
    backoff = 5.0  # a delay that would overshoot it by an order of magnitude

    def slow_timeout(_req: httpx.Request) -> httpx.Response:
        time.sleep(0.02)
        raise httpx.ReadTimeout("simulated read timeout")

    recorder = CountingRecorder(slow_timeout)
    provider = provider_for(
        recorder,
        max_transport_retries=max_transport_retries,
        timeout=timeout,
        retry_backoff_base=backoff,
        retry_backoff_cap=backoff,
    )

    started = time.monotonic()
    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(_REQUEST)
    elapsed = time.monotonic() - started

    assert excinfo.value.message == "request to the model endpoint timed out"
    # The overshooting backoff was never slept for.
    assert elapsed < 2.0


async def test_a_request_whose_budget_is_already_spent_is_never_sent() -> None:
    """The budget is checked before the request, not after it.

    A request that leaves no budget at all is refused before a socket is
    touched: the loop must not spend a request it has no time for. The error
    it surfaces is the timeout itself, with nothing chained behind it —
    because nothing was tried.

    This is the half of the budget that the *other* half cannot cover: the
    "no time left to retry" guard only ever fires after a failure, so a
    budget that was already spent when the loop started is this guard's
    business alone.
    """
    recorder = CountingRecorder(always(openai_chat_response("pong")))

    provider = provider_for(recorder, max_transport_retries=2, timeout=10.0)
    request = CompletionRequest(messages=(Message.user("ping"),), timeout=0.0)

    with pytest.raises(ModelUnavailableError) as excinfo:
        await provider.complete(request)

    assert recorder.count == 0
    assert excinfo.value.message == "request to the model endpoint timed out"
    assert excinfo.value.details["timeout_seconds"] == 0.0
    assert excinfo.value.__cause__ is None
