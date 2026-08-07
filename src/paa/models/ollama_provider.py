"""Ollama provider — the default local tier.

SPEC DEVIATION (docs/adr/0007): the RFC's local tier is vLLM. Ollama replaces
it because it is the only local server that installs and runs unattended on the
target machine: a single Windows binary, CPU inference with automatic GPU
offload where a GPU exists, no CUDA toolchain, and GGUF quantisation small
enough for ~3.5 GB of free RAM. The trade is throughput — Ollama has no
continuous batching and no paged attention, so it is materially slower than
vLLM under concurrency. RFC §6.2 caps concurrent streams at 2 on this hardware
anyway, which is where vLLM's advantage would have come from.

Ollama's ``format`` parameter is the reason this is the preferred local
provider rather than merely an available one. Passing a JSON Schema there
engages grammar-constrained decoding in llama.cpp: the sampler cannot emit a
token that would make the output diverge from the schema. That is a structural
guarantee about *syntax*, not a request — which matters enormously for a 3B
model, whose failure mode is otherwise confidently-malformed JSON.
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

__all__ = ["OllamaProvider"]

log = structlog.get_logger(__name__)

#: Ollama's own default. Loopback, and deliberately never configurable to a
#: non-loopback host without the caller saying so explicitly.
DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaProvider(HttpModelProvider):
    """Chat completions from a local Ollama daemon."""

    def __init__(
        self,
        model: str = "qwen2.5:3b-instruct",
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        keep_alive: str | None = "5m",
    ) -> None:
        super().__init__(
            base_url=base_url, timeout=timeout, max_retries=max_retries, client=client
        )
        self._model = model
        self._keep_alive = keep_alive

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def tier(self) -> ModelTier:
        return ModelTier.LOCAL

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = self._build_body(request)
        started = time.perf_counter()
        payload = await self._post_json("/api/chat", body, request_timeout=request.timeout)
        latency_ms = (time.perf_counter() - started) * 1000.0

        message = payload.get("message")
        if not isinstance(message, dict):
            raise ModelUnavailableError(
                "ollama response contained no message object",
                provider=self.name,
                model=self._model,
                keys=sorted(payload.keys()),
            )

        return CompletionResponse(
            text=str(message.get("content") or ""),
            model=str(payload.get("model") or self._model),
            # Ollama names these differently from every other API. Mapping them
            # here rather than at the call site is what keeps the router's token
            # accounting provider-agnostic.
            prompt_tokens=_as_count(payload.get("prompt_eval_count")),
            completion_tokens=_as_count(payload.get("eval_count")),
            finish_reason=_as_optional_str(payload.get("done_reason")),
            latency_ms=latency_ms,
            provider=self.name,
        )

    def _build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Translate a neutral request into Ollama's ``/api/chat`` body.

        Unlike Anthropic, Ollama *does* take the system prompt as a message with
        ``role: "system"``, so the messages pass through unmodified.

        ``stream: false`` is mandatory here, not a preference: with streaming on,
        Ollama returns newline-delimited JSON objects and ``response.json()``
        would fail on the second one.
        """
        options: dict[str, Any] = {
            "temperature": request.temperature,
            "num_predict": request.max_tokens,
        }
        if request.stop:
            options["stop"] = list(request.stop)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": False,
            "options": options,
        }
        if self._keep_alive is not None:
            # Without this the weights are evicted between calls and every
            # request pays the model load again — seconds, on this hardware.
            body["keep_alive"] = self._keep_alive
        if request.json_schema is not None:
            # A schema object (not the string "json") engages grammar-constrained
            # decoding. See the module docstring.
            body["format"] = request.json_schema
        return body

    async def healthcheck(self) -> bool:
        """Whether the daemon is up, via ``/api/tags``.

        Returns ``False`` rather than raising when Ollama is not running, which
        is the *expected* state on a machine where the user has not started it.
        ``/api/tags`` is used rather than a trial generation because it neither
        loads a model nor consumes tokens.
        """
        try:
            client = await self._http()
            response = await client.get(f"{self.base_url}/api/tags", timeout=5.0)
        except Exception as exc:
            log.debug("ollama.healthcheck_failed", error=str(exc), base_url=self.base_url)
            return False
        return response.status_code == 200

    async def list_models(self) -> list[str]:
        """Model tags the daemon has pulled. Empty when it is unreachable."""
        try:
            client = await self._http()
            response = await client.get(f"{self.base_url}/api/tags", timeout=5.0)
            models = response.json().get("models") or []
        except Exception as exc:
            log.debug("ollama.list_models_failed", error=str(exc))
            return []
        return [str(m.get("name")) for m in models if isinstance(m, dict) and m.get("name")]


def _as_count(value: Any) -> int:
    """Coerce a reported token count, treating anything odd as "not reported".

    Never estimates. See :class:`~paa.models.base.CompletionResponse`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _as_optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None
