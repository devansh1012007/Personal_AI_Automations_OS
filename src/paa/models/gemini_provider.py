"""Google Gemini provider — a second frontier option.

Per ADR-0015 the frontier tier is where reasoning material may leave the
machine, and ADR-0007 makes capability a configuration choice rather than an
architectural commitment. Gemini earns a bespoke provider for the same reason
Anthropic does: its wire format is *not* the OpenAI shape, and pretending it is
would corrupt every request.

Three differences carry the whole implementation:

* **Roles.** The conversation lives under ``contents`` with roles ``"user"`` and
  ``"model"`` (not ``"assistant"``). The system prompt is a *top-level*
  ``systemInstruction`` object, exactly the porting mistake called out in
  :class:`~paa.models.anthropic_provider.AnthropicProvider`.
* **Structured output.** There is no tool-call detour and no ``response_format``
  block. Setting ``generationConfig.responseMimeType`` to ``application/json``
  with a ``responseSchema`` puts the decoder into constrained JSON mode, so the
  generic parse-validate-retry loop in
  :class:`~paa.models.base.ModelProvider.complete_structured` is the right
  safety net and is reused unchanged.
* **Auth.** The key goes in the ``x-goog-api-key`` header, never the query
  string — a credential in a URL would defeat ``_safe_url`` and land in a log.
  It is read from the environment at call time, stored only by variable *name*,
  and passed through :func:`~paa.models.base.redact` before any response body
  can reach an exception.
"""

from __future__ import annotations

import os
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

__all__ = ["DEFAULT_BASE_URL", "GeminiProvider"]

log = structlog.get_logger(__name__)

#: The v1beta REST surface is the one that exposes ``responseSchema``. Pinned
#: for the same reason Anthropic's version header is: response shapes must not
#: drift under a runtime that replays historical traces.
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Environment variables consulted in order. Both are conventional for Google's
#: generative APIs; the first one set wins.
DEFAULT_API_KEY_ENVS: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

#: JSON Schema keywords Gemini's ``responseSchema`` understands. Anything else
#: (``additionalProperties``, ``const``, ``$schema``, ``minItems`` ...) is
#: dropped rather than forwarded: the API rejects unknown keys outright, so a
#: passthrough would turn a valid PAA schema into a hard 400.
_GEMINI_SCHEMA_KEYS: frozenset[str] = frozenset(
    {"type", "format", "description", "nullable", "enum", "properties", "required", "items"}
)


class GeminiProvider(HttpModelProvider):
    """Gemini via ``POST /models/{model}:generateContent``."""

    def __init__(
        self,
        model: str = "gemini-2.0-flash",
        *,
        api_key_env: str | tuple[str, ...] = DEFAULT_API_KEY_ENVS,
        base_url: str | None = None,
        tier: ModelTier = ModelTier.FRONTIER,
        timeout: float = 120.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            max_retries=max_retries,
            client=client,
        )
        self._model = model
        self._api_key_envs = (api_key_env,) if isinstance(api_key_env, str) else tuple(api_key_env)
        self._tier = tier

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def tier(self) -> ModelTier:
        return self._tier

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_key_envs(self) -> tuple[str, ...]:
        """The *names* of the candidate environment variables. Never a value."""
        return self._api_key_envs

    @property
    def has_credentials(self) -> bool:
        return any(os.environ.get(name) for name in self._api_key_envs)

    def _api_key(self) -> str:
        for name in self._api_key_envs:
            if key := os.environ.get(name):
                return key
        raise ModelUnavailableError(
            "no API key is configured for the gemini provider",
            provider=self.name,
            # The variable *names* are the actionable, safe part of the message.
            api_key_envs=list(self._api_key_envs),
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        api_key = self._api_key()
        started = time.perf_counter()
        payload = await self._post_json(
            f"/models/{self._model}:generateContent",
            self._build_body(request),
            headers={"content-type": "application/json", "x-goog-api-key": api_key},
            request_timeout=request.timeout,
            secrets=(api_key,),
        )
        latency_ms = (time.perf_counter() - started) * 1000.0

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ModelUnavailableError(
                "gemini response contained no candidates",
                provider=self.name,
                model=self._model,
                keys=sorted(payload.keys()),
            )

        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        usage = payload.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}

        return CompletionResponse(
            text=_extract_text(candidate),
            model=str(payload.get("modelVersion") or self._model),
            prompt_tokens=_as_count(usage.get("promptTokenCount")),
            completion_tokens=_as_count(usage.get("candidatesTokenCount")),
            finish_reason=_finish_reason(candidate.get("finishReason")),
            latency_ms=latency_ms,
            provider=self.name,
        )

    def _build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Translate a neutral request into the ``generateContent`` body.

        ``assistant`` turns become ``model`` turns; system turns are lifted into
        ``systemInstruction`` because Gemini rejects a system role inside
        ``contents``.
        """
        contents = [
            {"role": _ROLE_MAP[m.role], "parts": [{"text": m.content}]}
            for m in request.conversation
        ]
        generation_config: dict[str, Any] = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_tokens,
        }
        if request.stop:
            generation_config["stopSequences"] = list(request.stop)
        if request.json_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = _to_gemini_schema(request.json_schema)

        body: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
        if (system := request.system_prompt) is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    async def healthcheck(self) -> bool:
        """Whether escalation to Gemini is *possible*. Never raises.

        Credential-presence only, mirroring
        :meth:`~paa.models.anthropic_provider.AnthropicProvider.healthcheck`: a
        probe request would cost tokens on every startup and a rate limit would
        be indistinguishable from a missing key. Reachability failures surface
        at the first genuine escalation, attributable to a task.
        """
        return self.has_credentials


#: Neutral roles → Gemini roles. The whole reason this provider is bespoke.
_ROLE_MAP: dict[str, str] = {"user": "user", "assistant": "model"}


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a PAA JSON Schema onto the subset ``responseSchema`` accepts.

    Recursive and total: unknown keywords are dropped, ``type`` is upper-cased to
    the proto enum form Gemini expects, and nested ``properties``/``items`` are
    projected too. The result is always a valid Gemini schema, never a 400 in
    disguise.
    """
    if not isinstance(schema, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, str):
            out["type"] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {
                name: _to_gemini_schema(sub) for name, sub in value.items() if isinstance(sub, dict)
            }
        elif key == "items" and isinstance(value, dict):
            out["items"] = _to_gemini_schema(value)
        else:
            out[key] = value
    return out


def _extract_text(candidate: dict[str, Any]) -> str:
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))


def _finish_reason(value: Any) -> str | None:
    """Normalise Gemini's ``STOP`` / ``MAX_TOKENS`` to lower case.

    Lower-casing is not cosmetic: ``"MAX_TOKENS".lower()`` is ``"max_tokens"``,
    which is exactly the token
    :attr:`~paa.models.base.CompletionResponse.truncated` looks for, so
    truncation detection keeps working across providers for free.
    """
    return value.lower() if isinstance(value, str) and value else None


def _as_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
