"""Shared HTTP plumbing for the network-backed providers.

Private module. Three providers speak HTTP to three different APIs, and the
parts that are *not* API-specific — client ownership, timeout policy, error
translation, secret scrubbing — must not drift apart between them, because the
one that drifts is the one that leaks a key into a traceback.

Client ownership is the subtle part. A provider may be handed an
:class:`httpx.AsyncClient` (which is how the tests inject
:class:`httpx.MockTransport` and get network-free request-shaping assertions),
or it may create its own lazily. Only a client it created does it close, so
``aclose()`` on a provider can never sever a client its caller is still using.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from paa.models.base import ModelProvider, ModelUnavailableError, redact

__all__ = ["HttpModelProvider"]

log = structlog.get_logger(__name__)


class HttpModelProvider(ModelProvider):
    """Base for providers that talk to an HTTP endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 120.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(max_retries=max_retries)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

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
        """
        client = await self._http()
        url = f"{self._base_url}{path}"
        effective_timeout = request_timeout if request_timeout is not None else self._timeout

        try:
            response = await client.post(
                url, json=body, headers=headers or {}, timeout=effective_timeout
            )
        except httpx.TimeoutException as exc:
            raise ModelUnavailableError(
                "request to the model endpoint timed out",
                provider=self.name,
                url=self._safe_url(url),
                timeout_seconds=effective_timeout,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"could not reach the model endpoint: {type(exc).__name__}",
                provider=self.name,
                url=self._safe_url(url),
            ) from exc

        if response.status_code >= 400:
            raise ModelUnavailableError(
                f"model endpoint returned HTTP {response.status_code}",
                provider=self.name,
                url=self._safe_url(url),
                status_code=response.status_code,
                body_excerpt=redact(self._body_text(response)[:400], *secrets),
            )

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
