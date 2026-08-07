"""The inference contract every model provider implements.

SPEC DEVIATION (docs/adr/0007): RFC §6.1 pins the reasoning layer to vLLM
serving ``Llama-3-8B-Instruct-Q8_0.gguf`` at 85% of VRAM, with a concurrent
``Mistral-7B`` critic. vLLM publishes Linux-only wheels and requires CUDA or
ROCm; the target machine is Windows 11 with a 2 GB AMD 660M iGPU and ~3.5 GB
free RAM, against which a Q8 8B model needs roughly 8.5 GB of weights. The
stack in the RFC cannot be installed, let alone run.

This module therefore defines a **provider-agnostic** contract. Per ADR-0015
the runtime is local-first with explicit, ledger-logged escalation, and
capability becomes a configuration choice rather than an architectural
commitment: swapping the model is a config change, not a rewrite.

Two things in here are load-bearing beyond plumbing:

:func:`validate_against_schema`
    Structured output is where small models fail most often, and they fail
    *plausibly* — well-formed JSON with a missing field or a string where an
    integer belongs. Every structured call is validated before the caller sees
    it, so a malformed generation becomes a retry (and then an escalation)
    rather than a ``KeyError`` three layers up.

:func:`redact`
    Provider errors routinely echo request material back. An API key must never
    reach a log line, a ledger payload or an exception message, so scrubbing
    happens at the boundary rather than being left to each call site to
    remember.
"""

from __future__ import annotations

import abc
import enum
import json
import re
from collections.abc import AsyncIterator
from typing import Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from paa.core.errors import PaaError

__all__ = [
    "CompletionRequest",
    "CompletionResponse",
    "Message",
    "ModelProvider",
    "ModelTier",
    "ModelUnavailableError",
    "StructuredOutputError",
    "extract_json_object",
    "minimal_instance_for_schema",
    "redact",
    "validate_against_schema",
]

log = structlog.get_logger(__name__)

Role = Literal["system", "user", "assistant"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelUnavailableError(PaaError):
    """A provider could not serve a request at all.

    This is the *routable* failure: it is what tells
    :class:`~paa.models.router.EscalatingModelRouter` that the local model is
    not going to produce an answer and escalation should be considered.
    Connection refused, HTTP 5xx, timeouts, and missing credentials all land
    here. A provider that answered but answered badly raises
    :class:`StructuredOutputError` instead — the distinction matters because
    only one of the two is worth retrying against the same provider.
    """

    def __init__(self, message: str, *, provider: str, **details: Any) -> None:
        super().__init__(redact(message), provider=provider, **details)
        self.provider = provider


class StructuredOutputError(PaaError):
    """A provider answered, but the answer did not conform to the schema.

    Carries ``schema_errors`` verbatim so the ledger records *why* the
    generation was rejected. ``raw_excerpt`` is truncated and redacted: the
    offending text is the single most useful debugging artefact here, and also
    the most likely place for a prompt to have carried something sensitive.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        schema_errors: list[str] | None = None,
        attempts: int = 1,
        raw_excerpt: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            redact(message),
            provider=provider,
            schema_errors=schema_errors or [],
            attempts=attempts,
            raw_excerpt=redact(raw_excerpt[:400]) if raw_excerpt else None,
            **details,
        )
        self.provider = provider
        self.schema_errors = schema_errors or []
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

#: Token shapes that must never survive into a log line or an exception.
#: Deliberately generic: this has to catch a credential we were never handed —
#: one echoed back inside a provider's own error body, for instance — not only
#: the key this process happens to hold.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|x-api-key|authorization)\b\s*[:=]\s*\S+", re.IGNORECASE),
)

_REDACTED: Final[str] = "***REDACTED***"

#: Below this length a "secret" is more likely to be a common substring than a
#: credential, and blind replacement would corrupt unrelated text.
_MIN_REDACTABLE_LENGTH: Final[int] = 8


def redact(text: str, *secrets: str | None) -> str:
    """Scrub credentials out of ``text``.

    Two passes, in this order. First the literal ``secrets`` the caller knows it
    is holding — this is exact and cannot miss. Then the generic patterns, which
    catch credentials that arrived from somewhere else.

    Applied unconditionally by :class:`ModelUnavailableError` and
    :class:`StructuredOutputError`, because "remember to redact at the raise
    site" is a rule that holds right up until the one path nobody tested.
    """
    if not text:
        return text
    out = text
    for secret in secrets:
        if secret and len(secret) >= _MIN_REDACTABLE_LENGTH:
            out = out.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class ModelTier(enum.IntEnum):
    """Capability class of a provider.

    ``IntEnum`` so ``provider.tier > ModelTier.LOCAL`` reads naturally — the
    ordering is the point, and it is what the router's escalation decision and
    the ledger's ``MODEL_ESCALATED`` payload are both expressed in terms of.
    """

    LOCAL = 0
    """Runs on this machine. No egress, no per-token cost, limited reasoning."""

    FRONTIER = 1
    """A hosted model. Reasoning calls leave the machine — see ADR-0015."""


class Message(BaseModel):
    """One turn of a conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role="assistant", content=content)


class CompletionRequest(BaseModel):
    """One inference request, in provider-neutral form.

    ``temperature`` defaults to ``0.0``. This is an agent runtime whose outputs
    gate filesystem mutations and whose failures are replayed from a ledger;
    sampling variance would make a replay diverge from the run it is supposed to
    reproduce. Callers that genuinely want diversity (plan branch expansion)
    raise it deliberately.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[Message, ...]
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    stop: tuple[str, ...] = ()

    json_schema: dict[str, Any] | None = None
    """JSON Schema the response must satisfy. When set, providers use their
    native constrained-decoding path (Ollama ``format``, OpenAI
    ``response_format``, Anthropic tool-call) rather than asking politely."""

    timeout: float | None = None
    """Per-request wall clock. ``None`` defers to the provider's default."""

    @property
    def system_prompt(self) -> str | None:
        """System messages joined, or ``None``.

        Extracted rather than passed through because the Anthropic Messages API
        takes ``system`` as a *top-level* parameter and rejects ``"system"`` as
        a message role. Doing the split here means every provider agrees on what
        the system prompt is, instead of each one re-deriving it.
        """
        parts = [m.content for m in self.messages if m.role == "system"]
        return "\n\n".join(parts) if parts else None

    @property
    def conversation(self) -> tuple[Message, ...]:
        """Messages with the system turns removed."""
        return tuple(m for m in self.messages if m.role != "system")

    def with_schema(self, schema: dict[str, Any] | None) -> CompletionRequest:
        return self.model_copy(update={"json_schema": schema})

    def with_messages(self, messages: tuple[Message, ...]) -> CompletionRequest:
        return self.model_copy(update={"messages": messages})


class CompletionResponse(BaseModel):
    """One inference result.

    Token counts are ``0`` when a provider does not report them. That is a
    deliberate under-count rather than an estimate: the observability layer sums
    these into a budget, and a fabricated number would be indistinguishable from
    a measured one at the point where it mattered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    finish_reason: str | None = None
    latency_ms: float = Field(default=0.0, ge=0.0)
    provider: str = "unknown"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        """Whether generation stopped because it hit ``max_tokens``.

        Worth checking before parsing: a truncated response is the usual cause
        of "valid-looking JSON that ends mid-object".
        """
        return self.finish_reason in {"length", "max_tokens", "max_output_tokens"}

    def to_payload(self) -> dict[str, Any]:
        """Ledger-safe summary. Excludes the generated text, which can be large."""
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "finish_reason": self.finish_reason,
            "latency_ms": round(self.latency_ms, 3),
        }


# ---------------------------------------------------------------------------
# JSON Schema — a deliberate subset
#
# `jsonschema` is not a dependency and is not being added: the core dependency
# list is kept small on purpose (see pyproject), and the schemas this runtime
# emits are ones it also authors. The supported keyword set below covers every
# construct a skill contract or a planner output actually uses. Anything
# unrecognised is *ignored*, never guessed at — a validator that invents
# semantics for a keyword it does not implement is worse than one that admits
# the gap, because the caller cannot tell which happened.
# ---------------------------------------------------------------------------

_TYPE_CHECKS: Final[dict[str, Any]] = {
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python, so `isinstance(True, int)` is True.
    # Left unguarded, `{"type": "integer"}` would happily accept `true`.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def validate_against_schema(
    instance: Any, schema: dict[str, Any], *, path: str = "$"
) -> list[str]:
    """Validate ``instance`` against ``schema``. Returns human-readable errors.

    Returns a *list* rather than raising so that a retry prompt can quote every
    problem at once. Feeding a model one error at a time makes it fix that one
    and break another; showing all of them converges in a single retry far more
    often.

    Supported: ``type`` (single or list), ``enum``, ``const``, ``properties``,
    ``required``, ``additionalProperties`` (boolean form), ``items``,
    ``minItems``/``maxItems``, ``minLength``/``maxLength``,
    ``minimum``/``maximum``, ``anyOf``/``oneOf``/``allOf``. Everything else is
    ignored; see the module comment above.
    """
    errors: list[str] = []
    if not isinstance(schema, dict) or not schema:
        return errors

    declared = schema.get("type")
    if declared is not None:
        options = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS.get(name, lambda _: True)(instance) for name in options):
            actual = "null" if instance is None else type(instance).__name__
            errors.append(f"{path}: expected type {'|'.join(options)}, got {actual}")
            # A type mismatch invalidates every other keyword at this level, and
            # continuing would emit a cascade of derived noise.
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected the constant {schema['const']!r}, got {instance!r}")

    if (allowed := schema.get("enum")) is not None and instance not in allowed:
        errors.append(f"{path}: {instance!r} is not one of {allowed!r}")

    for keyword in ("allOf", "anyOf", "oneOf"):
        if (branches := schema.get(keyword)) is not None:
            errors.extend(_validate_combinator(instance, branches, keyword, path))

    if isinstance(instance, dict):
        errors.extend(_validate_object(instance, schema, path))
    elif isinstance(instance, list):
        errors.extend(_validate_array(instance, schema, path))
    elif isinstance(instance, str):
        errors.extend(_validate_string(instance, schema, path))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        errors.extend(_validate_number(instance, schema, path))

    return errors


def _validate_combinator(
    instance: Any, branches: Any, keyword: str, path: str
) -> list[str]:
    if not isinstance(branches, list):
        return []
    results = [
        validate_against_schema(instance, b, path=path)
        for b in branches
        if isinstance(b, dict)
    ]
    if not results:
        return []
    passing = sum(1 for r in results if not r)
    if keyword == "allOf" and passing != len(results):
        return [e for r in results for e in r]
    if keyword == "anyOf" and passing == 0:
        return [f"{path}: matched none of the {len(results)} anyOf branches"]
    if keyword == "oneOf" and passing != 1:
        return [f"{path}: matched {passing} oneOf branches, expected exactly 1"]
    return []


def _validate_object(instance: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties") or {}

    for name in schema.get("required") or []:
        if name not in instance:
            errors.append(f"{path}: missing required property {name!r}")

    if schema.get("additionalProperties") is False and properties:
        for name in instance:
            if name not in properties:
                errors.append(f"{path}: unexpected property {name!r}")

    for name, sub_schema in properties.items():
        if name in instance and isinstance(sub_schema, dict):
            errors.extend(
                validate_against_schema(instance[name], sub_schema, path=f"{path}.{name}")
            )
    return errors


def _validate_array(instance: list[Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if (minimum := schema.get("minItems")) is not None and len(instance) < minimum:
        errors.append(f"{path}: expected at least {minimum} items, got {len(instance)}")
    if (maximum := schema.get("maxItems")) is not None and len(instance) > maximum:
        errors.append(f"{path}: expected at most {maximum} items, got {len(instance)}")
    if isinstance(item_schema := schema.get("items"), dict):
        for index, item in enumerate(instance):
            errors.extend(validate_against_schema(item, item_schema, path=f"{path}[{index}]"))
    return errors


def _validate_string(instance: str, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if (minimum := schema.get("minLength")) is not None and len(instance) < minimum:
        errors.append(f"{path}: shorter than minLength {minimum}")
    if (maximum := schema.get("maxLength")) is not None and len(instance) > maximum:
        errors.append(f"{path}: longer than maxLength {maximum}")
    return errors


def _validate_number(instance: float, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if (minimum := schema.get("minimum")) is not None and instance < minimum:
        errors.append(f"{path}: {instance} is below minimum {minimum}")
    if (maximum := schema.get("maximum")) is not None and instance > maximum:
        errors.append(f"{path}: {instance} is above maximum {maximum}")
    return errors


def minimal_instance_for_schema(schema: dict[str, Any], *, name: str | None = None) -> Any:
    """Build the smallest value that satisfies ``schema``.

    Used by :class:`~paa.models.echo_provider.EchoProvider` to honour a schema
    offline, and by the retry path as a shape hint. The output is *valid*, not
    *meaningful* — every string is a placeholder.

    When ``required`` is present only those properties are emitted, which is the
    literal minimum. When it is absent, ``{}`` would technically satisfy the
    schema but would make the echo provider useless as a stand-in, so every
    declared property is filled instead. Extra declared properties never
    invalidate an object, so both branches are correct.
    """
    if not isinstance(schema, dict) or not schema:
        return {}

    if "const" in schema:
        return schema["const"]
    if (allowed := schema.get("enum")):
        return allowed[0]
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches and isinstance(branches[0], dict):
            return minimal_instance_for_schema(branches[0], name=name)

    declared = schema.get("type")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    if declared is None:
        declared = "object" if "properties" in schema else "string"

    if declared == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required")
        wanted = list(required) if required else list(properties)
        return {
            key: minimal_instance_for_schema(properties.get(key) or {}, name=key)
            for key in wanted
        }
    if declared == "array":
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        count = int(schema.get("minItems") or 0)
        return [minimal_instance_for_schema(item_schema or {}) for _ in range(count)]
    if declared == "integer":
        return int(schema.get("minimum") or 0)
    if declared == "number":
        return float(schema.get("minimum") or 0.0)
    if declared == "boolean":
        return False
    if declared == "null":
        return None

    value = name or "echo"
    minimum = int(schema.get("minLength") or 0)
    while len(value) < minimum:
        value += "x"
    maximum = schema.get("maxLength")
    return value[:maximum] if maximum is not None else value


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL | re.IGNORECASE
)


def extract_json_object(text: str) -> Any:
    """Pull a JSON value out of a model response.

    Small models wrap JSON in markdown fences or prefix it with "Here is the
    JSON:" no matter how firmly the prompt says not to. Refusing those responses
    would burn a retry (and eventually an escalation) on a formatting habit
    rather than a reasoning failure, so three strategies are tried in order of
    decreasing confidence: parse the whole string, parse the contents of a
    fenced block, then parse the outermost balanced ``{}``/``[]`` span.

    Raises :class:`ValueError` when none of them yield JSON.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("response was empty")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    if (fenced := _FENCE_RE.search(stripped)) is not None:
        try:
            return json.loads(fenced.group("body").strip())
        except json.JSONDecodeError:
            pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError("no parseable JSON value found in the response")


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


class ModelProvider(abc.ABC):
    """Contract for anything that can turn a prompt into text.

    Implementations must be safe to call concurrently — the router fans several
    requests at one provider under a semaphore — and must hold no per-request
    state on the instance.
    """

    def __init__(self, *, max_retries: int = 2) -> None:
        self._max_retries = max(0, int(max_retries))

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable identifier written into ledger payloads (``"ollama"``, ...)."""

    @property
    @abc.abstractmethod
    def tier(self) -> ModelTier:
        """Capability class. Must be honest — the router's privacy decision
        (does this call leave the machine?) is made from it."""

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Model identifier this provider is configured to call."""

    @property
    def max_retries(self) -> int:
        """Structured-output repair attempts before giving up."""
        return self._max_retries

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Generate a completion.

        Raises :class:`ModelUnavailableError` when the provider could not serve
        the request at all. Must not raise for a *poor* answer — that is the
        caller's judgement to make.
        """

    @abc.abstractmethod
    async def healthcheck(self) -> bool:
        """Whether this provider can serve work *right now*.

        Must never raise. Startup wiring and ``paa doctor`` both call it to
        choose between providers, and a healthcheck that throws would take down
        provider selection itself — the exact moment a truthful ``False`` is
        most useful.
        """

    async def complete_structured(
        self, request: CompletionRequest, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate a completion that conforms to ``schema``.

        The generic implementation: ask with the schema attached (so the
        provider's own constrained-decoding path engages), parse, validate, and
        on failure re-ask with the errors quoted back. Up to
        :attr:`max_retries` repairs, then :class:`StructuredOutputError`.

        Providers with a stronger native mechanism override this —
        :class:`~paa.models.anthropic_provider.AnthropicProvider` uses a forced
        tool call, which conforms far more reliably than any amount of asking.

        Validation is *not* skipped when a provider claims constrained
        decoding. Every one of them is best-effort in practice, and an unchecked
        claim is exactly the failure this method exists to stop.
        """
        attempt_request = request.with_schema(schema)
        errors: list[str] = []
        raw = ""

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                attempt_request = _repair_request(attempt_request, raw, errors)

            response = await self.complete(attempt_request)
            raw = response.text
            try:
                candidate = extract_json_object(raw)
            except ValueError as exc:
                errors = [str(exc)]
                log.debug(
                    "model.structured_parse_failed",
                    provider=self.name,
                    attempt=attempt,
                    error=str(exc),
                )
                continue

            errors = validate_against_schema(candidate, schema)
            if not errors:
                return candidate if isinstance(candidate, dict) else {"value": candidate}

            log.debug(
                "model.structured_invalid",
                provider=self.name,
                attempt=attempt,
                error_count=len(errors),
            )

        raise StructuredOutputError(
            "provider could not produce output conforming to the schema",
            provider=self.name,
            schema_errors=errors,
            attempts=self.max_retries + 1,
            raw_excerpt=raw,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Yield the completion as a sequence of text deltas.

        The default implementation is non-streaming: it awaits
        :meth:`complete` and yields the whole body as one chunk. This keeps
        ``stream`` callable on *every* provider — including
        :class:`~paa.models.echo_provider.EchoProvider` and any backend whose
        server does not support incremental delivery — so a caller can write to
        the streaming contract without branching on provider capability.

        Providers backed by a token-streaming endpoint override this to yield
        real deltas as they arrive; the neutral contract is only that the
        concatenation of the chunks equals the completion text. Errors surface
        exactly as in :meth:`complete` — :class:`ModelUnavailableError` for a
        provider that could not answer at all.
        """
        response = await self.complete(request)
        if response.text:
            yield response.text

    def describe(self) -> dict[str, Any]:
        """Provider identity for the ledger and ``paa doctor``."""
        return {
            "provider": self.name,
            "model": self.model,
            "tier": self.tier.name,
            "leaves_machine": self.tier >= ModelTier.FRONTIER,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r}, tier={self.tier.name})"


def _repair_request(
    request: CompletionRequest, raw: str, errors: list[str]
) -> CompletionRequest:
    """Append the failed generation and its errors as a repair turn.

    Showing the model its own bad output alongside the specific complaints is
    markedly more effective than re-sending the original prompt, which mostly
    reproduces the same mistake. The excerpt is capped so a runaway generation
    cannot blow the context window on the retry.
    """
    complaint = "\n".join(f"- {e}" for e in errors[:10]) or "- output was not valid JSON"
    return request.with_messages(
        (
            *request.messages,
            Message.assistant(raw[:2000]),
            Message.user(
                "That response did not satisfy the required JSON schema:\n"
                f"{complaint}\n\n"
                "Reply with the corrected JSON object only. No prose, no markdown fences."
            ),
        )
    )
