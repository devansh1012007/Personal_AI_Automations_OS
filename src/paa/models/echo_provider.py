"""Deterministic offline provider. Zero network.

This is what the test suite runs against, and it is a first-class part of the
runtime rather than a fixture: ``ModelSettings.local_provider`` accepts
``"echo"``, so ``paa`` boots and exercises its full orchestration path on a
machine with no Ollama, no API key and no network at all.

Determinism is the whole contract. The same request always produces the same
response, byte for byte, which is what makes the ledger's replay guarantee
testable — a replayed lineage must reproduce its recorded state, and it cannot
if the model layer is a source of variance. Nothing here reads a clock,
generates a random number, or touches a socket.

What it does **not** do is reason. Every response is a template or a canned
string. Tests that assert on *content* must supply that content through
``responses``; tests that assert on *plumbing* — routing, escalation, token
accounting, concurrency — get everything they need from the defaults.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog

from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    ModelProvider,
    ModelTier,
    ModelUnavailableError,
    minimal_instance_for_schema,
)

__all__ = ["EchoProvider"]

log = structlog.get_logger(__name__)

#: Characters per token for the synthetic counts. The same 4.0 approximation
#: ``ContextSettings.chars_per_token`` uses, so a budget assertion written
#: against the echo provider stays meaningful against a real one.
_CHARS_PER_TOKEN = 4.0


class EchoProvider(ModelProvider):
    """A model-shaped object that never calls a model.

    :param responses: exact-match canned replies, keyed on the final user
        message. The way a test pins content.
    :param fail_times: raise :class:`~paa.models.base.ModelUnavailableError` on
        the first *n* calls. Present so router escalation tests can produce a
        genuine local failure without a mock — the router's behaviour on a
        failing local provider is the single most important thing it does.
    :param healthy: what :meth:`healthcheck` reports.
    """

    def __init__(
        self,
        *,
        name: str = "echo",
        model: str = "echo-1",
        tier: ModelTier = ModelTier.LOCAL,
        responses: dict[str, str] | None = None,
        fail_times: int = 0,
        healthy: bool = True,
        max_retries: int = 0,
        prefix: str = "[echo]",
    ) -> None:
        super().__init__(max_retries=max_retries)
        self._name = name
        self._model = model
        self._tier = tier
        self._responses = dict(responses or {})
        self._remaining_failures = max(0, int(fail_times))
        self._healthy = healthy
        self._prefix = prefix
        self.calls: list[CompletionRequest] = []
        """Every request this provider was handed, in order. Tests assert on it."""

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> ModelTier:
        return self._tier

    @property
    def model(self) -> str:
        return self._model

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)

        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise ModelUnavailableError(
                "echo provider was configured to fail this call",
                provider=self._name,
                model=self._model,
                remaining_failures=self._remaining_failures,
            )

        text = self._render(request)
        prompt_chars = sum(len(m.content) for m in request.messages)

        return CompletionResponse(
            text=text,
            model=self._model,
            prompt_tokens=int(prompt_chars / _CHARS_PER_TOKEN),
            completion_tokens=int(len(text) / _CHARS_PER_TOKEN),
            finish_reason="stop",
            # Deliberately 0.0 rather than a measured elapsed time: a real
            # duration would make otherwise-identical responses differ, and
            # replay-equality tests compare whole response objects.
            latency_ms=0.0,
            provider=self._name,
        )

    def _render(self, request: CompletionRequest) -> str:
        """Produce the response body. Pure function of the request."""
        if request.json_schema is not None:
            # Canonical JSON — sorted keys and fixed separators — so the exact
            # bytes are reproducible across runs and platforms.
            return json.dumps(
                minimal_instance_for_schema(request.json_schema),
                sort_keys=True,
                separators=(",", ":"),
            )

        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"), ""
        )
        if last_user in self._responses:
            return self._responses[last_user]

        # A digest of the whole request, so two different prompts never collide
        # into the same reply and a test can assert "this response came from
        # that request" without pinning the literal text.
        material = "␟".join(f"{m.role}:{m.content}" for m in request.messages)
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        return f"{self._prefix}[{digest}] {last_user}".rstrip()

    async def healthcheck(self) -> bool:
        return self._healthy

    def queue_failures(self, count: int) -> None:
        """Arm the next ``count`` calls to fail. Additive."""
        self._remaining_failures += max(0, int(count))

    def reset(self) -> None:
        """Clear recorded calls and pending failures."""
        self.calls.clear()
        self._remaining_failures = 0

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "deterministic": True, "network": False}
