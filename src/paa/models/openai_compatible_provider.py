"""OpenAI-compatible ``/v1/chat/completions`` provider.

One class covers a surprising amount of ground, because ``/v1/chat/completions``
became the de facto local-inference interface: LM Studio, ``llama-server`` from
llama.cpp, text-generation-webui, LiteLLM and other gateways, and vLLM itself
if the user ever runs this on Linux with a supported GPU (ADR-0007 explains why
that is not the target machine, not that it is forbidden).

That last case matters for the RFC's intent. The spec's inference stack is not
unreachable forever — point ``base_url`` at a vLLM server and this provider
speaks to it unmodified, at whichever tier the caller declares. The tier is a
constructor argument rather than a constant for exactly this reason: the same
protocol serves a 3B model on loopback and a hosted frontier gateway, and only
the deployer knows which one is behind the URL. Getting that wrong would make
the router's privacy accounting wrong, so it is not guessed at.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from paa.models._http import HttpModelProvider
from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    ModelTier,
    ModelUnavailableError,
)

__all__ = ["OpenAICompatibleProvider"]

log = structlog.get_logger(__name__)


class OpenAICompatibleProvider(HttpModelProvider):
    """Chat completions from any OpenAI-shaped endpoint."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        tier: ModelTier = ModelTier.LOCAL,
        name: str = "openai_compatible",
        timeout: float = 120.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        schema_name: str = "structured_output",
        chat_path: str = "/v1/chat/completions",
        models_path: str = "/v1/models",
        auth_header: str = "authorization",
        auth_scheme: str = "Bearer",
        extra_query: str | None = None,
    ) -> None:
        """Construct a provider for any OpenAI-shaped endpoint.

        The five path/auth knobs exist because "OpenAI-compatible" is a family,
        not a single wire format (see :mod:`paa.models.registry`). Azure OpenAI
        keys off ``api-key`` rather than ``Authorization: Bearer`` and pins the
        deployment in the path plus an ``api-version`` query string; Perplexity
        serves ``/chat/completions`` with no ``/v1`` prefix. Encoding those as
        constructor arguments — all defaulting to the canonical OpenAI shape —
        keeps one class covering the whole family instead of a subclass per
        vendor quirk, and leaves the default behaviour byte-identical.
        """
        super().__init__(
            base_url=base_url, timeout=timeout, max_retries=max_retries, client=client
        )
        self._model = model
        self._api_key = api_key
        self._tier = tier
        self._name = name
        self._schema_name = schema_name
        self._chat_path = chat_path
        self._models_path = models_path
        self._auth_header = auth_header
        self._auth_scheme = auth_scheme
        self._extra_query = extra_query

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> ModelTier:
        return self._tier

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        started = time.perf_counter()
        payload = await self._post_json(
            self._with_query(self._chat_path),
            self._build_body(request),
            headers=self._headers(),
            request_timeout=request.timeout,
            secrets=(self._api_key,),
        )
        latency_ms = (time.perf_counter() - started) * 1000.0

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelUnavailableError(
                "response contained no choices",
                provider=self.name,
                model=self._model,
                keys=sorted(payload.keys()),
            )

        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

        return CompletionResponse(
            text=str(message.get("content") or ""),
            model=str(payload.get("model") or self._model),
            prompt_tokens=_as_count(usage.get("prompt_tokens")),
            completion_tokens=_as_count(usage.get("completion_tokens")),
            finish_reason=_as_optional_str(choice.get("finish_reason")),
            latency_ms=latency_ms,
            provider=self.name,
        )

    def _with_query(self, path: str) -> str:
        """Append the fixed query string (Azure's ``api-version``) if any.

        Kept off the base URL so the query never lands in the connection pool
        key and is stripped from any logged URL by ``_safe_url``.
        """
        return f"{path}?{self._extra_query}" if self._extra_query else path

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            # Azure passes a bare key under ``api-key``; everyone else uses
            # ``Authorization: Bearer <key>``. An empty scheme means "send the
            # key verbatim", which is what the header value collapses to here.
            headers[self._auth_header] = (
                f"{self._auth_scheme} {self._api_key}" if self._auth_scheme else self._api_key
            )
        return headers

    def _build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Translate a neutral request into the chat-completions body.

        System turns stay in ``messages`` with ``role: "system"`` — the opposite
        of Anthropic, and the reason both translations live in their own
        provider rather than in a shared helper that would need a flag.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.stop:
            body["stop"] = list(request.stop)
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": self._schema_name,
                    "schema": request.json_schema,
                    # `strict` engages the server's grammar constraint where it
                    # is supported. Servers that do not know the key ignore it,
                    # which is why the generic validate-and-retry loop in
                    # ModelProvider.complete_structured is still the safety net.
                    "strict": True,
                },
            }
        return body

    async def healthcheck(self) -> bool:
        """Probe ``/v1/models``. Never raises.

        A 401 counts as healthy-but-unauthorised only in the sense that the
        server answered; it is reported as ``False`` because a provider that
        will reject every generation is not usable, and the router needs the
        actionable answer rather than the technically-accurate one.
        """
        try:
            client = await self._http()
            response = await client.get(
                f"{self.base_url}{self._with_query(self._models_path)}",
                headers=self._headers(),
                timeout=5.0,
            )
        except Exception as exc:
            log.debug(
                "openai_compatible.healthcheck_failed",
                error=str(exc),
                base_url=self.base_url,
                provider=self.name,
            )
            return False
        return response.status_code == 200


def _as_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _as_optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None
