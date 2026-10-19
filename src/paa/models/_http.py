"""Shared HTTP plumbing for the network-backed providers.

Private module. Three providers speak HTTP to three different APIs, and the
parts that are *not* API-specific — client ownership, timeout policy, error
translation, secret scrubbing, transport retry — must not drift apart between
them, because the one that drifts is the one that leaks a key into a traceback
or swallows a transient 429 as if it were fatal.

Client ownership is the subtle part. A provider may be handed an
:class:`httpx.AsyncClient` (which is how the tests inject
:class:`httpx.MockTransport` and get network-free request-shaping assertions),
or it may create its own lazily. Only a client it created does it close, so
``aclose()`` on a provider can never sever a client its caller is still using.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Never

import httpx
import structlog

from paa.models.base import ModelProvider, ModelUnavailableError, redact

__all__ = ["HttpModelProvider"]

log = structlog.get_logger(__name__)

#: HTTP statuses that mean "the same request could still be served a moment
#: later". 408 and 429 are the standards' own promises of transience; 5xx is
#: the usual gateway/overload class. Every other 4xx is the *caller's* fault —
#: a 401 re-sent one second later is still a 401, only slower — and that
#: distinction is the whole point of a narrow retryable set: a loop that
#: retried those too would turn a config error into a slow config error.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429})

#: ``Retry-After`` is honoured only up to this many seconds. The header is the
#: server's suggestion, not this runtime's budget: a gateway answering "retry
#: in ten minutes" must not park a call whose own wall clock is
#: ``request_timeout_seconds * (max_transport_retries + 1)``
#: (SPEC DEVIATION (docs/adr/0021)).
_RETRY_AFTER_CAP_S: float = 10.0

#: Above this exponent the backoff schedule is already orders of magnitude past
#: the cap for any sane base, while ``2.0 ** n`` overflows a float well before
#: it — a pathological retry count must degrade to "wait the cap", not crash.
_MAX_BACKOFF_EXPONENT: int = 62


def _raise_chained(error: ModelUnavailableError, cause: BaseException | None) -> Never:
    """Raise ``error``, chaining the transport exception behind it.

    Factored out so the retry loop can decide *when* a failure surfaces
    without changing the shape of what a caller sees: a
    ``raise X from exc`` inside an ``except`` block and a deferred
    ``raise X from exc`` outside one produce the same message, the same
    ``__cause__`` and the same traceback rendering — only the timing of the
    raise moves, which is exactly what the retry loop is allowed to change
    (SPEC DEVIATION (docs/adr/0021)).
    """
    if cause is None:
        raise error
    raise error from cause


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """``Retry-After`` as seconds, in either of the header's two legal forms.

    RFC 9110 allows delta-seconds (``120``) and an HTTP-date
    (``Wed, 21 Oct 2026 07:28:00 GMT``). Both are understood: the date form
    is resolved against the current wall clock. Both forms are clamped to
    :data:`_RETRY_AFTER_CAP_S` exactly alike — the header is the server's
    suggestion, not this runtime's budget — and a date already in the past
    yields ``0.0``, because the retry it asks for is due: a negative sleep is
    not a thing.

    Absent, malformed, negative or non-finite values all return ``None``:
    the exponential schedule is the fallback, and sleeping on a header we
    guessed at is worse than sleeping on the one we understood. Callers
    consult this only on 429 — the status where the header is part of the
    contract rather than an occasional courtesy on 503.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        dated = _retry_after_date_seconds(raw)
        if dated is None:
            return None
        return min(dated, _RETRY_AFTER_CAP_S)
    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return min(seconds, _RETRY_AFTER_CAP_S)


def _retry_after_date_seconds(raw: str) -> float | None:
    """The HTTP-date form of ``Retry-After``, as seconds from now.

    ``None`` when the value is not a date we can read at all. A date in the
    past yields ``0.0`` rather than a negative number of seconds: the server
    is asking for a retry that is already due.
    """
    try:
        when = parsedate_to_datetime(raw.strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class HttpModelProvider(ModelProvider):
    """Base for providers that talk to an HTTP endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 120.0,
        max_retries: int = 2,
        max_transport_retries: int = 2,
        retry_backoff_base: float = 0.5,
        retry_backoff_cap: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Base constructor for the HTTP-backed providers.

        ``max_retries`` (see :class:`~paa.models.base.ModelProvider`) repairs
        malformed *structured output*; it never protects the request itself.
        ``max_transport_retries`` is the other half — how many times a
        transient transport failure (timeout, connection error, HTTP
        408/429/5xx) is re-attempted before the provider is declared
        unavailable (SPEC DEVIATION (docs/adr/0021)). The distinction is not
        cosmetic: a single 429 from a hosted gateway used to end the whole
        run, and escalation policy can forbid the fallback that would
        otherwise have absorbed it. Those attempts share a wall-clock budget
        of ``timeout * (max_transport_retries + 1)`` — the timeout bounds a
        single attempt, not the whole loop; see :meth:`_post_json`.

        The backoff knobs are constructor arguments rather than settings
        because they are timing constants, not operational choices; tests set
        them to ~0 so retry coverage runs instantly instead of sleeping half a
        second per attempt.
        """
        super().__init__(max_retries=max_retries)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_transport_retries = max(0, int(max_transport_retries))
        self._retry_backoff_base = max(0.0, retry_backoff_base)
        self._retry_backoff_cap = max(0.0, retry_backoff_cap)
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def max_transport_retries(self) -> int:
        """Transient transport failures re-attempted before giving up (ADR-0021)."""
        return self._max_transport_retries

    async def _http(self) -> httpx.AsyncClient:
        """The client, created on first use.

        Not built in ``__init__``: constructing an ``AsyncClient`` outside a
        running loop is legal but binds connection-pool state that a later
        ``asyncio.run`` in a different loop will not reuse cleanly. Provider
        construction happens during synchronous startup wiring, so the client
        is deferred to the first call, which is always inside the loop that
        will use it.
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=self._timeout)
                self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Release the client, but only if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
        secrets: tuple[str | None, ...] = (),
    ) -> dict[str, Any]:
        """POST ``body`` as JSON and return the decoded response.

        Every failure mode collapses to :class:`ModelUnavailableError`, because
        the router's decision is binary — did this provider answer or not — and
        a caller forced to distinguish ``httpx.ConnectError`` from
        ``httpx.ReadTimeout`` from ``json.JSONDecodeError`` would get it wrong.

        Response bodies are echoed into the error (truncated) since a 400 from a
        model API almost always says exactly what was malformed. They are
        scrubbed first: a provider that rejects a request frequently quotes the
        request back, headers included.

        Transient failures — timeouts, connection errors and HTTP 408/429/5xx —
        are re-attempted up to :attr:`max_transport_retries` times with capped
        exponential backoff (a ``Retry-After`` on 429 overrides it, in either
        of its two legal forms), all bounded by a wall-clock deadline so
        retries cannot multiply the call's latency without limit (SPEC
        DEVIATION (docs/adr/0021)). The bound is
        ``request_timeout_seconds * (max_transport_retries + 1)``: the request
        timeout is a *per-attempt* budget, and the loop's own budget is that
        timeout times the number of attempts it is allowed to make. The two
        must not be the same quantity — an attempt that genuinely times out
        consumes its whole timeout, so a deadline equal to that timeout would
        leave the retry the knob exists to buy with nowhere to run, and a
        timeout would never be retried at all. Deterministic failures — any
        other 4xx, a non-JSON body, a JSON value that is not an object — raise
        on the first response exactly as they did before this loop existed:
        replaying a 401 returns a 401. What the caller eventually sees is
        byte-for-byte the same message, ``status_code`` and redacted
        ``body_excerpt`` as a one-shot failure; only *when* it surfaces has
        changed. The retry log line carries the status but never the body,
        because a rejection body can quote the key back.
        """
        client = await self._http()
        url = f"{self._base_url}{path}"
        effective_timeout = request_timeout if request_timeout is not None else self._timeout
        max_attempts = self._max_transport_retries + 1
        # The loop's budget is the *per-attempt* timeout times the attempt
        # count — deliberately not the timeout itself. An attempt that really
        # times out spends the whole per-attempt timeout, so a deadline equal
        # to that timeout would be spent by the first attempt and the retry
        # could never run: the timeout half of the retryable class would be
        # advertised and unreachable. Two quantities, one deadline
        # (SPEC DEVIATION (docs/adr/0021)).
        deadline = time.monotonic() + effective_timeout * max_attempts

        last_error: ModelUnavailableError | None = None
        last_cause: BaseException | None = None

        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                # The budget ran out during the previous backoff sleep, or the
                # caller asked for a non-positive timeout. The last failure is
                # still the honest, fully-shaped error to surface; with no
                # failure yet there is nothing to chain, so the timeout is
                # reported as itself rather than as a request with no timeout.
                if last_error is not None:
                    _raise_chained(last_error, last_cause)
                raise ModelUnavailableError(
                    "request to the model endpoint timed out",
                    provider=self.name,
                    url=self._safe_url(url),
                    timeout_seconds=effective_timeout,
                )

            error: ModelUnavailableError
            cause: BaseException | None
            retryable: bool
            retry_after: float | None
            status_code: int | None

            try:
                response = await client.post(
                    url,
                    json=body,
                    headers=headers or {},
                    timeout=min(effective_timeout, remaining),
                )
            except httpx.TimeoutException as exc:
                error = ModelUnavailableError(
                    "request to the model endpoint timed out",
                    provider=self.name,
                    url=self._safe_url(url),
                    timeout_seconds=effective_timeout,
                )
                cause = exc
                retryable = True
                retry_after = None
                status_code = None
            except httpx.HTTPError as exc:
                error = ModelUnavailableError(
                    f"could not reach the model endpoint: {type(exc).__name__}",
                    provider=self.name,
                    url=self._safe_url(url),
                )
                cause = exc
                retryable = True
                retry_after = None
                status_code = None
            else:
                if response.status_code < 400:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ModelUnavailableError(
                            "model endpoint returned a non-JSON body",
                            provider=self.name,
                            url=self._safe_url(url),
                            body_excerpt=redact(self._body_text(response)[:400], *secrets),
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ModelUnavailableError(
                            "model endpoint returned a JSON value that is not an object",
                            provider=self.name,
                            url=self._safe_url(url),
                        )
                    return payload

                status_code = response.status_code
                error = ModelUnavailableError(
                    f"model endpoint returned HTTP {status_code}",
                    provider=self.name,
                    url=self._safe_url(url),
                    status_code=status_code,
                    body_excerpt=redact(self._body_text(response)[:400], *secrets),
                )
                cause = None
                retryable = status_code in _RETRYABLE_STATUSES or status_code >= 500
                retry_after = _retry_after_seconds(response) if status_code == 429 else None

            if not retryable or attempt >= max_attempts - 1:
                _raise_chained(error, cause)

            if retry_after is not None:
                delay = retry_after
            else:
                exponent = min(attempt, _MAX_BACKOFF_EXPONENT)
                delay = min(self._retry_backoff_base * (2.0**exponent), self._retry_backoff_cap)

            if time.monotonic() + delay >= deadline:
                # Sleeping would leave no time to actually try again — the
                # budget is a hard bound on wall clock, not a hint. Raise the
                # error in hand rather than burning the wait and then failing
                # with a request that had no time left to run.
                _raise_chained(error, cause)

            log.warning(
                "model.http_retry",
                provider=self.name,
                attempt=attempt + 1,
                status_code=status_code,
                delay_s=delay,
                url=self._safe_url(url),
            )
            last_error, last_cause = error, cause
            await asyncio.sleep(delay)

        raise AssertionError("unreachable: the retry loop always returns or raises")

    @staticmethod
    def _body_text(response: httpx.Response) -> str:
        try:
            return response.text
        except Exception:  # pragma: no cover - undecodable body
            return "<undecodable body>"

    @staticmethod
    def _safe_url(url: str) -> str:
        """Strip the query string before a URL reaches an error or a log.

        Some OpenAI-compatible gateways accept the key as ``?api_key=``. That is
        their choice, but it must not become a credential in our ledger.
        """
        return url.split("?", 1)[0]
