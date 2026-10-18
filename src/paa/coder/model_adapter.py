"""A real :class:`~paa.coder.loop.ToolCallingModel` driven by the escalation router.

:mod:`paa.coder.loop` is deliberately model-agnostic: it asks for a
:class:`~paa.coder.loop.ToolCallingModel` and nothing else, which is why the
loop's permission and hook guarantees can be tested with a scripted double. What
was missing is the other end — an implementation that satisfies the protocol
against a real provider, so the same loop can be exercised for real. This module
is that adapter.

**Why constrained JSON rather than native tool calls.** The obvious
implementation would put a ``tools`` array on the wire and parse
``message.tool_calls``. Nothing in :mod:`paa.models` supports that: the
provider layer's one structured-output mechanism is
:meth:`~paa.models.base.ModelProvider.complete_structured`, which attaches a
JSON Schema and validates the reply. This adapter therefore asks the model for a
``{text, tool_calls[]}`` object conforming to a schema and lets the existing
parse-validate-repair loop do the guaranteeing. The trade is deliberate: tool
names are constrained by an ``enum`` in the schema, so a hallucinated tool name
is a validation failure the provider retries rather than a crash in the loop, and
the *same* code path serves Ollama's grammar-constrained decoding, Anthropic's
forced tool call, and OpenRouter's ``response_format`` without a per-provider
branch. Adding native tool calling later is an optimisation inside this one
class, invisible to :class:`~paa.coder.loop.AgentLoop`.

**Routing is not bypassed.** The adapter holds an
:class:`~paa.models.router.EscalatingModelRouter`, not a bare provider, so
every proposal goes through the same escalation policy, privacy accounting and
``MODEL_ESCALATED`` ledger audit as a planner call. A coding agent that quietly
reached around the router would be the one part of the runtime where a hosted
model's reasoning left the machine unlogged.

**Token counts are reported as zero.** ``complete_structured`` returns a parsed
``dict`` and discards the provider's usage numbers, so :attr:`ModelTurn.tokens`
stays ``0``. That is a real under-count rather than an estimate, matching the
rule in :class:`~paa.models.base.CompletionResponse`: a fabricated number would
be indistinguishable from a measured one in the budget that sums them. The
consequence is that :class:`~paa.coder.loop.AgentLoop`'s ``max_tokens`` budget
does not trip on this path — use ``max_steps`` to bound a run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from paa.coder.loop import ModelTurn, ToolCall
from paa.core.types import ComplexityModality, PermissionMode
from paa.models.base import CompletionRequest, Message

if TYPE_CHECKING:
    from paa.core.types import CorrelationId, SessionId
    from paa.models.router import EscalatingModelRouter

__all__ = ["ROUTER_TOOLCALLING_SYSTEM_PROMPT", "RouterToolCallingModel", "turn_schema"]

log = structlog.get_logger(__name__)


ROUTER_TOOLCALLING_SYSTEM_PROMPT = """\
You are the operator console for the PAA runtime. You drive a local-first,
event-sourced agent substrate by calling tools and reading what comes back.

Rules:
- Call a tool only when you need information you do not have, or you need to
  change state. Never guess a correlation id, a phase, or an event type; read
  them from a tool result.
- Every task you submit lands in an append-only hash-chained ledger. Its events
  are the only source of truth; queues, indexes and the filesystem are derived
  views rebuilt by replay. When a result looks wrong, read the ledger for that
  correlation id before concluding anything.
- Report what actually happened. If a tool reports an error, surface the error
  rather than a summary of what you hoped happened.
- When the work is done, call no tools and put the answer in `text`.
"""


def turn_schema(tool_names: list[str]) -> dict[str, Any]:
    """The schema for one model turn, constrained to the tools actually offered.

    ``tool_names`` becomes an ``enum`` so the provider's constrained decoding
    physically cannot emit a name the loop would not find. An empty catalogue
    degenerates to the no-tools case: return ``tool_calls: []`` and answer.
    """

    name_field: dict[str, Any] = {"type": "string"}
    if tool_names:
        name_field["enum"] = list(tool_names)
    return {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Narration for the operator. Empty string if none.",
            },
            "tool_calls": {
                "type": "array",
                "maxItems": 8,
                "description": "Tools to run before your next turn. Empty to finish.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": name_field,
                        "arguments": {
                            "type": "object",
                            "description": "Arguments matching the tool's input schema.",
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
        "required": ["text", "tool_calls"],
    }


class RouterToolCallingModel:
    """Satisfies ``ToolCallingModel`` on top of the escalation router."""

    def __init__(
        self,
        router: EscalatingModelRouter,
        *,
        system_prompt: str | None = None,
        instructions: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        permission_mode: PermissionMode = PermissionMode.ASK,
        correlation_id: CorrelationId | None = None,
        session_id: SessionId | None = None,
        force_escalate: bool = False,
    ) -> None:
        """Wire the adapter.

        ``instructions`` is appended to the system prompt; it is how a caller
        teaches the operator console about this particular deployment (which
        PAA_HOME, which policy mode) without replacing the base prompt.

        The modality/permission pair is passed to *every* proposal, so the
        router's own rules apply uniformly: with the default
        ``ComplexityModality.STANDARD`` a floor of ``COMPLEX`` means no
        escalation, and a non-``FRONTIER``-permitted permission mode means no
        escalation either. Raise ``modality`` deliberately rather than
        forcing it per call, so the decision stays auditable.
        """

        self._router = router
        self._system_prompt = "\n\n".join(
            p for p in (system_prompt or ROUTER_TOOLCALLING_SYSTEM_PROMPT, instructions) if p
        )
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._modality = modality
        self._permission_mode = permission_mode
        self._correlation_id = correlation_id
        self._session_id = session_id
        self._force_escalate = force_escalate

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        """One think step: render the window, ask the router, parse the turn."""
        catalogue = json.dumps(tools, indent=2, sort_keys=True)
        messages: tuple[Message, ...] = (
            Message.system(f"{self._system_prompt}\n\n## Tools\n\n{catalogue}"),
            *self._render(window),
        )
        request = CompletionRequest(
            messages=messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )

        raw = await self._router.complete_structured(
            request,
            turn_schema([str(t.get("name", "")) for t in tools if t.get("name")]),
            modality=self._modality,
            permission_mode=self._permission_mode,
            correlation_id=self._correlation_id,
            session_id=self._session_id,
            reason="interactive coder agent turn",
            force_escalate=self._force_escalate,
        )
        return self._to_turn(raw)

    def _render(self, window: list[dict[str, Any]]) -> tuple[Message, ...]:
        """Flatten the loop's neutral window into provider messages.

        ``Message`` admits only system/user/assistant, so tool traffic is
        rendered as labelled user turns. They are the model's *observations*,
        not instructions, and the labels say so — the loop guarantees the
        ordering, which is what matters for a model reading the transcript back.
        """
        rendered: list[Message] = []
        for entry in window:
            role = entry.get("role")
            if role == "assistant":
                rendered.append(Message.assistant(str(entry.get("content") or "")))
            elif role == "user":
                rendered.append(Message.user(str(entry.get("content") or "")))
            elif role == "summary":
                rendered.append(
                    Message.user(f"[summary of earlier conversation]\n{entry.get('content', '')}")
                )
            elif role == "tool_call":
                args = json.dumps(entry.get("arguments") or {}, sort_keys=True)
                rendered.append(
                    Message.user(f"[tool call] {entry.get('name', '?')}({args})")
                )
            elif role == "tool_result":
                rendered.append(
                    Message.user(
                        f"[tool result] {entry.get('name', '?')}\n{entry.get('content', '')}"
                    )
                )
        return tuple(rendered)

    def _to_turn(self, raw: dict[str, Any]) -> ModelTurn:
        """Validate-by-construction parse of a turn the schema already checked.

        The schema is not a guarantee on its own: ``additionalProperties`` is
        left open so tool arguments stay free-form, and a provider that ignores
        ``response_format`` (which every one of them may, per
        :meth:`~paa.models.base.ModelProvider.complete_structured`) can still
        return the wrong types. So the parsing is defensive rather than
        trusting, and a malformed field degrades to "no tool calls" — which the
        loop reads as *finished*, ending the run instead of raising. That is the
        right failure direction for an unattended test agent: it stops and
        reports, rather than spinning on a response it cannot read.
        """
        text = raw.get("text")
        calls: list[ToolCall] = []
        for entry in raw.get("tool_calls") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                log.warning("coder_agent.malformed_tool_call", entry=str(entry)[:200])
                continue
            calls.append(ToolCall(name=name, arguments=_as_object(entry.get("arguments"))))
        return ModelTurn(text=text if isinstance(text, str) else "", tool_calls=calls)


def _as_object(value: Any) -> dict[str, Any]:
    """Coerce a tool's ``arguments`` to a mapping.

    Models routinely emit ``"{\"cid\": \"...\"}"`` — the arguments as a JSON
    *string* — even under a schema that declares them as an object. The loop's
    permission policy stringifies arguments before matching anyway, so a string
    here would silently fail every argument-pattern rule; decoding it keeps the
    permission gate doing its job.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except ValueError:
            return {"value": value}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {}