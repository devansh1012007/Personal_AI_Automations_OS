"""Anthropic Messages API provider — the frontier tier.

Per ADR-0015 this is the only component in the runtime that sends reasoning
material off the machine, and only when
:class:`~paa.models.router.EscalatingModelRouter` decides a task has earned it.
Memory, ledger, workspaces and telemetry never leave.

Two implementation decisions worth stating.

**No SDK.** The ``anthropic`` package is not a dependency. This provider calls
the HTTP API directly through httpx, which is already a core dependency. The
surface used here is four headers and one JSON body; adding a package (and its
transitive tree) to a runtime budgeted for ~3.5 GB of RAM to avoid writing that
down is a bad trade.

**Secret discipline.** The API key is read from the environment at call time,
is never stored on the instance beyond the variable *name*, never appears in a
log line, and is passed through :func:`~paa.models.base.redact` before any
response body can reach an exception. :meth:`healthcheck` returns ``False``
when no key is configured rather than raising, so a machine with no credential
degrades to local-only operation instead of crashing at startup.
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
    StructuredOutputError,
    validate_against_schema,
)

__all__ = ["ANTHROPIC_VERSION", "AnthropicProvider"]

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Pinned deliberately. The Messages API is versioned by this header, and
#: letting it float would mean a server-side release could change response
#: shapes under a runtime that replays historical traces.
ANTHROPIC_VERSION = "2023-06-01"

#: Name of the synthetic tool used to force schema conformance. Arbitrary, but
#: stable — it appears in the response and is matched on.
_STRUCTURED_TOOL = "emit_result"


class AnthropicProvider(HttpModelProvider):
    """Claude via ``POST /v1/messages``."""

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        *,
        api_key_env: str = "ANTHROPIC_API_KEY",
        base_url: str | None = None,
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
        self._api_key_env = api_key_env

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def tier(self) -> ModelTier:
        return ModelTier.FRONTIER

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_key_env(self) -> str:
        """The *name* of the environment variable. Never its value."""
        return self._api_key_env

    @property
    def has_credentials(self) -> bool:
        return bool(os.environ.get(self._api_key_env))

    def _api_key(self) -> str:
        key = os.environ.get(self._api_key_env)
        if not key:
            raise ModelUnavailableError(
                "no API key is configured for the escalation provider",
                provider=self.name,
                # The variable *name* is safe and is the actionable part of the
                # message; the value is what must never appear.
                api_key_env=self._api_key_env,
            )
        return key

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # -- text ---------------------------------------------------------------

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        api_key = self._api_key()
        body = self._build_body(request)
        if request.json_schema is not None:
            # A schema on a plain complete() still deserves the reliable path.
            self._attach_tool(body, request.json_schema)

        payload = await self._send(body, api_key, request.timeout)
        return self._to_response(payload, latency_ms=payload.pop("_latency_ms", 0.0))

    # -- structured ---------------------------------------------------------

    async def complete_structured(
        self, request: CompletionRequest, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Force schema conformance with a tool call.

        Overrides the generic parse-and-retry loop in
        :class:`~paa.models.base.ModelProvider` because the tool path is
        categorically more reliable than prompting: declaring the schema as a
        tool's ``input_schema`` and pinning ``tool_choice`` to that tool makes
        the model emit a structured ``tool_use`` block, so there is no prose to
        strip, no markdown fence to unwrap, and no free-text JSON to repair.

        The result is still validated. A tool call is strongly typed at the
        block level but the model can still omit an optional-looking required
        field, and an unchecked assumption here would surface as a ``KeyError``
        in whichever agent consumed the dict.
        """
        api_key = self._api_key()
        body = self._build_body(request.with_schema(None))
        self._attach_tool(body, schema)

        errors: list[str] = []
        raw = ""
        for attempt in range(self.max_retries + 1):
            payload = await self._send(body, api_key, request.timeout)
            payload.pop("_latency_ms", None)
            result = _extract_tool_input(payload)

            if result is None:
                raw = _extract_text(payload)
                errors = ["provider returned no tool_use block"]
                log.debug("anthropic.no_tool_use", attempt=attempt, model=self._model)
                continue

            errors = validate_against_schema(result, schema)
            if not errors:
                return result
            raw = str(result)[:400]
            log.debug("anthropic.tool_input_invalid", attempt=attempt, error_count=len(errors))

        raise StructuredOutputError(
            "anthropic tool call did not conform to the schema",
            provider=self.name,
            schema_errors=errors,
            attempts=self.max_retries + 1,
            raw_excerpt=raw,
            model=self._model,
        )

    # -- wire ---------------------------------------------------------------

    def _build_body(self, request: CompletionRequest) -> dict[str, Any]:
        """Translate a neutral request into the Messages API body.

        ``system`` is a **top-level parameter**, not a message with
        ``role: "system"``. The API rejects that role outright, so
        :attr:`~paa.models.base.CompletionRequest.system_prompt` lifts the
        system turns out and the remaining conversation is sent as ``messages``.
        This is the single most common thing to get wrong when porting a prompt
        from an OpenAI-shaped API.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.conversation
            ],
        }
        if (system := request.system_prompt) is not None:
            body["system"] = system
        if request.temperature:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        return body

    @staticmethod
    def _attach_tool(body: dict[str, Any], schema: dict[str, Any]) -> None:
        """Pin the response to a single tool whose input *is* the schema."""
        body["tools"] = [
            {
                "name": _STRUCTURED_TOOL,
                "description": "Return the result as structured data.",
                "input_schema": schema,
            }
        ]
        body["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL}

    async def _send(
        self, body: dict[str, Any], api_key: str, request_timeout: float | None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        payload = await self._post_json(
            "/v1/messages",
            body,
            headers=self._headers(api_key),
            request_timeout=request_timeout,
            # Belt and braces: _post_json already runs generic patterns over any
            # echoed body, and this pins the literal key we are holding.
            secrets=(api_key,),
        )
        payload["_latency_ms"] = (time.perf_counter() - started) * 1000.0
        return payload

    def _to_response(self, payload: dict[str, Any], *, latency_ms: float) -> CompletionResponse:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return CompletionResponse(
            text=_extract_text(payload),
            model=str(payload.get("model") or self._model),
            prompt_tokens=_as_count(usage.get("input_tokens")),
            completion_tokens=_as_count(usage.get("output_tokens")),
            finish_reason=_as_optional_str(payload.get("stop_reason")),
            latency_ms=latency_ms,
            provider=self.name,
        )

    async def healthcheck(self) -> bool:
        """Whether escalation is *possible*. Never raises, never logs the key.

        Deliberately credential-presence only — no network call is made. A
        healthcheck that burned a real request would cost money on every startup
        and every ``paa doctor``, and would fail on a rate limit in a way that
        looks identical to a missing key. Reachability failures surface at the
        first genuine escalation, where they are attributable to a task.
        """
        return self.has_credentials


# ---------------------------------------------------------------------------
# Response shape helpers
#
# Anthropic returns `content` as a *list of typed blocks*, not a string. Text
# and tool calls arrive as different block types in the same array.
# ---------------------------------------------------------------------------


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content = payload.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _extract_text(payload: dict[str, Any]) -> str:
    return "".join(
        str(b.get("text") or "") for b in _blocks(payload) if b.get("type") == "text"
    )


def _extract_tool_input(payload: dict[str, Any]) -> dict[str, Any] | None:
    for block in _blocks(payload):
        if block.get("type") == "tool_use" and block.get("name") == _STRUCTURED_TOOL:
            candidate = block.get("input")
            if isinstance(candidate, dict):
                return candidate
    return None


def _as_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _as_optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None
