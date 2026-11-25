"""The chat loop: built-in pipeline #2, one turn per run.

C4.1 of SPEC 2 §3.1. The per-turn flow is the plan's own line — hydrate →
assemble context → think → reply → extract signals → remember — with every
effect crossing the ADR-0051 facade and every state change ledgered before
it:

1. ``CHAT_TURN_PROPOSED`` (journal) — before any effect.
2. ``ctx.recall`` (semantic facts, global with provenance) +
   :meth:`ChatTurnFacade.episodic_turns` (this session's last-k turns) →
   :func:`paa.chat.context_bundle.build_context_bundle`. Each consult is a
   ``MEMORY_RECALL_CONSULTED`` row.
3. ``ctx.think`` — the model call, streaming deltas through the optional
   sink. A cancel anywhere from here on is shielded, recorded as
   ``CHAT_TURN_CANCELLED``, and re-raised: a cancelled turn is not a failed
   one, and swallowing the error would report a cancelled run as complete
   (the coder loop's cancel contract, conversation-shaped).
4. ``ctx.remember`` twice — the user's message (filed to the cold lake and
   extracted by the MemoryCreator into semantic facts,
   ``MEMORY_SIGNAL_EXTRACTED``) and the assistant's reply (filed for
   episodic recall only; extraction deliberately does not run on the
   assistant's own words — a model's paraphrase of memory must never become
   new memory).
5. ``CHAT_TURN_COMPLETED`` — proof, hashes and ids only.

Shape choice (the plan's noted fallback, taken deliberately): ``ChatLoop``
satisfies the frozen :class:`~paa.pipelines.base.Pipeline` protocol and
runs one turn per ``run(task, ctx)``, but the facade it consumes is *this*
module's :class:`ChatTurnFacade`, minted per turn by the host
(:class:`paa.chat.service.ChatService`), not the built-in's
``BuiltinPipelineFacade``. The reason is mechanical, not tactical: the
built-in facade hard-refuses ``think``/``recall``/``remember`` (its agents
own their models and their memory), its ``think`` is a
``complete_structured``-shaped call with no streaming surface, and its run
contract drives plan/step/policy machinery a chat turn does not have.
Minting a chat-shaped facade that implements the same frozen protocol keeps
the journal vocabulary identical to what a shared-facade shape would write,
so the W6 re-expression (C6.x) is a host change, not an event change.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import structlog

from paa.agents.base import AgentContext, AgentMessage, AgentResult
from paa.chat.context_bundle import PERSONA_SLOT, build_context_bundle
from paa.chat.session import CHAT_CHANNEL, ChatSessionJournal, text_sha256
from paa.config import ChatSettings, ContextSettings, get_settings
from paa.core.errors import PaaError
from paa.core.types import (
    AgentRole,
    ComplexityModality,
    Permission,
    PermissionMode,
)
from paa.ledger.replay import TaskProjection, project
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.memory.facts import Fact, FactRepository
from paa.models.base import CompletionRequest, Message, ModelProvider, redact
from paa.pipelines.base import (
    Capability,
    CapabilityNotGrantedError,
    CapabilitySet,
    GateQuestion,
    InvalidCallParamsError,
    PipelineArtifact,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.base import CapabilityFacade as _FacadeProtocol
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.base import RelationalBackend

__all__ = [
    "ChatLoop",
    "ChatModel",
    "ChatReply",
    "ChatTurnFacade",
    "DeltaSink",
    "EchoChatModel",
    "ProviderChatModel",
    "build_chat_model",
    "render_chat_prompt",
]

log = structlog.get_logger(__name__)

#: What the chat pipeline asks for. Three capabilities, all exercised every
#: turn; everything else the facade offers is refused by the grant set.
CHAT_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {Capability.RECALL, Capability.REMEMBER, Capability.THINK}
)

#: An async sink for reply deltas. ``None`` means "no streaming wanted".
DeltaSink = Callable[[str], Awaitable[None]]

#: The context-provider slot's callable shape (C6.4 / P1.5): what
#: :func:`paa.chat.context_bundle.build_context_bundle` satisfies and an
#: overlay's replacement must too. Stated as a plain callable because the
#: slot's contract is "same inputs, same bundle shape out", not a class.
ContextProvider = Callable[..., Any]

#: Delta size for models without a native token stream. Chunking a finished
#: reply is a transport presentation, not a claim about the provider: the
#: ledger records the final reply either way, and per-token SSE plumbing is
#: a provider concern this wave does not own.
_DELTA_CHARS: Final = 12

#: The echo reply's fact-note cap. The note exists so the deterministic
#: two-turn proof is visible in the reply itself; a long fact list would
#: make the reply a document, and echo is not a document renderer.
_ECHO_FACT_LIMIT: Final = 3

#: Characters per token for the echo reply's synthetic accounting — the same
#: 4.0 approximation ``ContextSettings.chars_per_token`` uses, so a budget
#: assertion written against the echo path stays meaningful against a real one.
_ECHO_CHARS_PER_TOKEN: Final = 4.0

_ROLE_USER: Final = "user"
_ROLE_ASSISTANT: Final = "assistant"

#: Characters stripped around recall probe tokens. Punctuation would defeat
#: the substring match ("Friday?" does not contain "Friday" for LIKE).
_PROBE_STRIP: Final = ".,!?:;\"'()"


@dataclass(frozen=True, slots=True)
class ChatReply:
    """One model reply: the text, and the model's own token accounting."""

    text: str
    tokens_used: int


class ChatModel(Protocol):
    """The chat model seam. Offline-deterministic by default (``echo``).

    The seam receives the *rendered* prompt (see :func:`render_chat_prompt`)
    and an optional delta sink. Implementations must be deterministic for
    the echo path and never read a clock or a socket on that path — the same
    contract :class:`~paa.models.echo_provider.EchoProvider` states for the
    provider layer, because replay equality dies where determinism does.
    """

    async def reply(self, prompt: str, *, on_delta: DeltaSink | None) -> ChatReply:
        """Produce one reply, streaming chunks through ``on_delta`` when given."""
        ...


def render_chat_prompt(
    *,
    persona: str,
    facts: Sequence[str],
    turns: Sequence[str],
    message: str,
) -> str:
    """The deterministic prompt render every chat model receives.

    Sectioned, so a model (and the echo model's parser) can tell the
    instructions from the served memory from the conversation from the
    user's new message. Pure function of its arguments — no clock, no
    randomness — which is what keeps a replayed turn's prompt byte-identical
    to the recorded one.
    """
    persona_section = persona.strip() or "(none)"
    facts_section = "\n".join(f"- {fact}" for fact in facts) if facts else "(none)"
    history_section = "\n".join(turns) if turns else "(none)"
    return (
        f"[persona]\n{persona_section}\n"
        f"\n[facts]\n{facts_section}\n"
        f"\n[history]\n{history_section}\n"
        f"\n[message]\n{message}"
    )


def _parse_sections(prompt: str) -> dict[str, list[str]]:
    """Split the house render back into its sections. Echo's parser only."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in prompt.splitlines():
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            current = line[1:-1]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


class EchoChatModel:
    """The offline, deterministic chat model. Zero network, zero clock.

    The chat surface's own echo, in the spirit of the registry's
    ``EchoProvider`` (deterministic, no socket, canned rendering) but
    conversation-shaped: it answers the user's message and, when the turn's
    recall served facts, says so — which is what makes the two-turn
    "turn 2 recalls turn 1's fact" proof visible in the reply text, not just
    in the ledger. ``PAA_CHAT__MODEL=echo`` selects it; the name resolves
    through the chat seam rather than the provider registry because a plain
    provider echo has no way to acknowledge served memory.
    """

    async def reply(self, prompt: str, *, on_delta: DeltaSink | None) -> ChatReply:
        sections = _parse_sections(prompt)
        message = "\n".join(sections.get("message", [])).strip()
        facts = [
            line[2:] for line in sections.get("facts", []) if line.startswith("- ")
        ]
        text = message
        if facts:
            noted = "; ".join(facts[:_ECHO_FACT_LIMIT])
            text = f"{message}\n(from memory: {noted})"

        for start in range(0, len(text), _DELTA_CHARS):
            await asyncio.sleep(0)  # cooperative: a cancel lands here, not later
            if on_delta is not None:
                await on_delta(text[start : start + _DELTA_CHARS])

        return ChatReply(text=text, tokens_used=max(1, int(len(text) / _ECHO_CHARS_PER_TOKEN)))


class ProviderChatModel:
    """A registry provider behind the chat seam.

    ``ChatSettings.model`` names a registry row; ``build_provider``
    constructs it and this class adapts it. Streaming chunks the finished
    reply (see the module constant) — the ledger records the final reply
    either way, and per-token provider streaming is not this wave's surface.
    """

    def __init__(self, provider: ModelProvider, *, max_tokens: int = 1024) -> None:
        self._provider = provider
        self._max_tokens = max_tokens

    async def reply(self, prompt: str, *, on_delta: DeltaSink | None) -> ChatReply:
        request = CompletionRequest(
            messages=(Message(role="user", content=prompt),),
            max_tokens=self._max_tokens,
        )
        response = await self._provider.complete(request)
        text = response.text
        for start in range(0, len(text), _DELTA_CHARS):
            await asyncio.sleep(0)
            if on_delta is not None:
                await on_delta(text[start : start + _DELTA_CHARS])
        return ChatReply(
            text=text,
            tokens_used=response.prompt_tokens + response.completion_tokens,
        )


def build_chat_model(settings: ChatSettings) -> ChatModel:
    """Resolve ``ChatSettings.model`` onto the chat seam.

    ``"echo"`` is the offline default. Any other name must resolve through
    the provider registry — a typo surfaces here, at wiring time, as
    :class:`~paa.models.registry.UnknownProviderError`, not as a
    mid-conversation model failure.
    """
    if settings.model == "echo":
        return EchoChatModel()
    from paa.models.registry import build_provider

    return ProviderChatModel(build_provider(settings.model))


# ---------------------------------------------------------------------------
# The host-minted facade (ADR-0051's door, chat-shaped)
# ---------------------------------------------------------------------------


class ChatTurnFacade:
    """The facade one chat turn runs on — the host grants, the loop calls.

    Implements the frozen :class:`~paa.pipelines.base.CapabilityFacade`
    protocol for the three capabilities chat asks for and refuses the rest
    with :class:`CapabilityNotGrantedError` (ADR-0051's only door): chat
    has no agents to call, no skills to run, no plan to submit, no human
    gate, and no commit — a stage that grew one of those reaches fails
    loudly instead of silently widening. ``think`` carries one extra
    keyword beyond the protocol (``on_delta``, the streaming sink); extra
    *optional* keyword parameters keep the protocol signature callable.

    Memory consults are recorded here, where they happen: ``recall`` and
    ``episodic_turns`` each append a ``MEMORY_RECALL_CONSULTED`` row, and
    ``remember`` appends ``MEMORY_SIGNAL_EXTRACTED`` — the auditable record
    of what memory served and what it refused, written through the session
    journal the host built.
    """

    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        turn_id: str,
        journal: ChatSessionJournal,
        ledger: LedgerStore,
        model: ChatModel,
        creator: MemoryCreator,
        facts: FactRepository,
        db: RelationalBackend,
        settings: ChatSettings,
        permission_mode: PermissionMode,
        signals: SignalRepository | None = None,
        context_settings: ContextSettings | None = None,
        context_provider: ContextProvider | None = None,
    ) -> None:
        self._session_id = session_id
        self._turn_id = turn_id
        self._journal = journal
        self._ledger = ledger
        self._model = model
        self._creator = creator
        self._facts = facts
        self._db = db
        self._signals = signals
        self._settings = settings
        self._context_provider = (
            context_provider if context_provider is not None else build_context_bundle
        )
        self._grants = CapabilitySet.mint(CHAT_CAPABILITIES, run_id=session_id)
        # One budget line: the operator's chat override, else the context
        # engine's planner ceiling — resolved once, at mint time.
        self._ceiling = settings.token_budget or (
            context_settings if context_settings is not None else get_settings().context
        ).token_ceiling
        self._agent_ctx = AgentContext(
            correlation_id=session_id,
            session_id=session_id,
            modality=ComplexityModality.STANDARD,
            permission_mode=permission_mode,
            tokens_budget=self._ceiling,
        )
        # This turn's memory writes, appended by ``remember`` and read back
        # by the loop for the COMPLETED payload. The protocol's ``remember``
        # returns ``None`` — the write's *outcome* is the ledger row — so
        # the facts the turn recorded travel on the per-turn facade instead
        # of in a widened return type.
        self._writes: list[dict[str, Any]] = []
        # Seeded from the session lineage (not a fresh uuid4): a replayed
        # session's ``random`` values match the original run's, which is the
        # determinism contract the built-in facade states for the same aid.
        self._rng_state = int(session_id.hex[:16], 16)

    # -- run scope ---------------------------------------------------------

    @property
    def correlation_id(self) -> uuid.UUID:
        return self._session_id

    @property
    def session_id(self) -> uuid.UUID:
        return self._session_id

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def modality(self) -> ComplexityModality:
        return self._agent_ctx.modality

    @property
    def permission_mode(self) -> PermissionMode:
        return self._agent_ctx.permission_mode

    @property
    def tokens_budget(self) -> int:
        return self._ceiling

    @property
    def agent_ctx(self) -> AgentContext:
        return self._agent_ctx

    @property
    def persona(self) -> str:
        return self._settings.persona

    @property
    def recall_k(self) -> int:
        return self._settings.recall_k

    @property
    def last_k_turns(self) -> int:
        return self._settings.last_k_turns

    @property
    def streaming(self) -> bool:
        return self._settings.streaming

    @property
    def writes(self) -> tuple[dict[str, Any], ...]:
        """This turn's memory writes so far, in the order they landed."""
        return tuple(self._writes)

    def build_context(
        self,
        *,
        persona: str,
        recalled_facts: list[dict[str, Any]],
        recent_turns: list[dict[str, Any]],
        token_ceiling: int,
    ) -> Any:
        """Run the context-provider slot: assemble this turn's packet.

        The indirection is the slot, not a convenience: the loop asks the
        facade (the host-minted object) so the provider the host resolved
        an overlay's ``context_provider`` ref to is the one that runs —
        with the shipped compiler as the default when no overlay filled
        the slot. Pure: same inputs, same packet.
        """
        return self._context_provider(
            persona=persona,
            recalled_facts=recalled_facts,
            recent_turns=recent_turns,
            token_ceiling=token_ceiling,
        )

    # -- capabilities --------------------------------------------------------

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
        on_delta: DeltaSink | None = None,
    ) -> dict[str, Any]:
        """One model call. Free-text only: chat does not complete structures."""
        self._grants.require(Capability.THINK)
        if schema is not None:
            # A structured completion is the task pipeline's shape. Refusing
            # it here keeps the door honest: a caller that needs a schema is
            # not having a conversation, and silently completing a structure
            # from a chat prompt would be a second extractor in the wrong
            # layer.
            raise InvalidCallParamsError(
                "chat think is a free-text call; schema-bearing calls are not chat-shaped"
            )
        del role, budget  # accepted for protocol shape; chat has one role, one budget
        reply = await self._model.reply(prompt, on_delta=on_delta)
        return {"text": reply.text, "tokens_used": reply.tokens_used}

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        """Semantic facts for this turn — global, with the origin stamped."""
        self._grants.require(Capability.RECALL)
        facts = await self._consult_semantic(query, k)
        await self._journal.recall_consulted(
            turn_id=self._turn_id,
            query=query,
            k=k,
            fact_ids=[str(fact["id"]) for fact in facts],
        )
        return facts

    async def remember(self, fact: Mapping[str, Any]) -> None:
        """One turn signal through the MemoryCreator (the protocol's memory write).

        ``{"kind": "user"|"assistant", "text": ...}``. A user signal is
        filed to the cold lake and extracted; an assistant signal is filed
        and marked processed *without* extraction — see the module docstring
        for why a model's paraphrase must not become memory. Returns
        ``None`` (the protocol's shape); the write's outcome lands on
        :attr:`writes` and in the ``MEMORY_SIGNAL_EXTRACTED`` row.
        """
        self._grants.require(Capability.REMEMBER)
        text = fact.get("text")
        if not isinstance(text, str) or not text:
            raise InvalidCallParamsError("remember requires a non-empty 'text' string")
        kind = fact.get("kind", _ROLE_USER)
        role = kind if kind in (_ROLE_USER, _ROLE_ASSISTANT) else _ROLE_USER
        payload: dict[str, Any] = {
            "session_id": str(self._session_id),
            "turn_id": self._turn_id,
            "role": role,
            "text": text,
        }
        if self._signals is None:
            # Degrade-never-raise: no cold lake, no episodic record, but the
            # semantic path still runs on an unpersisted envelope — the
            # creator's process_signal accepts a bare payload mapping.
            result = await self._creator.process_signal(payload)
            signal_id: str | None = None
            outcome = result.outcome.value
            facts_written = result.facts_written
        else:
            signal = await self._signals.record(
                CHAT_CHANNEL,
                payload,
                external_id=f"{self._session_id}:{self._turn_id}:{role}",
            )
            signal_id = signal.id
            if role == _ROLE_ASSISTANT:
                # Episodic only. The reply is the model speaking, not the
                # world asserting; extracting it would let a paraphrase of
                # memory (or a hallucination) write itself back as a new
                # fact. ``processed`` is the honest row status: this signal
                # has been handled, deliberately, by the turn itself.
                await self._signals.mark_processed(signal.id)
                outcome = "episodic_only"
                facts_written = []
            else:
                result = await self._creator.process_signal(signal.id)
                outcome = result.outcome.value
                facts_written = result.facts_written
        await self._journal.signal_extracted(
            turn_id=self._turn_id,
            signal_id=signal_id or f"ephemeral:{self._turn_id}:{role}",
            outcome=outcome,
            facts_written=facts_written,
        )
        self._writes.append(
            {
                "role": role,
                "signal_id": signal_id,
                "outcome": outcome,
                "facts_written": facts_written,
            }
        )

    async def episodic_turns(
        self, target_session_id: uuid.UUID, *, k: int
    ) -> list[dict[str, Any]]:
        """Raw turns of one session, oldest first — the isolation fence lives here.

        Reading *another* session's episodic record without the operator's
        ``cross_session_episodic`` flag is refused, and the refusal is a
        ``MEMORY_RECALL_CONSULTED`` row with ``refused=True`` — the ledger
        shows the fence worked, not merely that a call failed.
        """
        if (
            target_session_id != self._session_id
            and not self._settings.cross_session_episodic
        ):
            await self._journal.recall_consulted(
                turn_id=self._turn_id,
                query=f"episodic:{target_session_id}",
                k=k,
                fact_ids=[],
                refused=True,
                refusal_reason=(
                    "cross-session episodic read denied: "
                    "PAA_CHAT__CROSS_SESSION_EPISODIC is off"
                ),
            )
            raise PaaError(
                "cross-session episodic read requires PAA_CHAT__CROSS_SESSION_EPISODIC",
                requested_session=str(target_session_id),
                session=str(self._session_id),
            )

        turns = await self._read_episodic(target_session_id, k)
        await self._journal.recall_consulted(
            turn_id=self._turn_id,
            query=f"episodic:{target_session_id}",
            k=k,
            # Episodic consults serve turns, not facts; the served turn ids
            # ride the same list so ``returned`` stays truthful either way.
            fact_ids=[
                str(turn["turn_id"])
                for turn in turns
                if isinstance(turn.get("turn_id"), str)
            ],
        )
        return turns

    async def _read_episodic(
        self, target_session_id: uuid.UUID, k: int
    ) -> list[dict[str, Any]]:
        """The cold-lake read behind :meth:`episodic_turns`. Newest first, reversed."""
        if self._signals is None or k <= 0:
            return []
        found: list[dict[str, Any]] = []
        async for signal in self._signals.iter_by_channel(CHAT_CHANNEL, newest_first=True):
            try:
                payload: Any = json.loads(signal.raw_payload)
            except ValueError:
                continue  # a malformed row is skipped, not fatal
            if not isinstance(payload, Mapping):
                continue
            if payload.get("session_id") != str(target_session_id):
                continue
            text = payload.get("text")
            if not isinstance(text, str):
                continue
            found.append(
                {
                    "turn_id": payload.get("turn_id"),
                    "role": payload.get("role", _ROLE_USER),
                    "text": text,
                    "received_at": signal.received_at,
                }
            )
            if len(found) >= k:
                break
        found.reverse()
        return found

    async def _consult_semantic(self, query: str, k: int) -> list[dict[str, Any]]:
        """Probe the fact store and shape the recall result.

        The full query is probed first (a phrase match is the best evidence);
        individual tokens follow, longest first, so "What happens on
        Friday?" still finds the fact that says ``is Friday`` through
        ``search_facts_by_text``'s substring match. Quarantined facts — the
        records parked in ``hot_serving_unresolved_buffer`` awaiting a human
        — are excluded here and again by the bundle's marker filter.
        """
        if k <= 0:
            return []
        quarantined = await self._quarantined_fact_ids()
        results: dict[str, Fact] = {}
        for probe in _recall_probes(query):
            found = await self._facts.search_facts_by_text(probe, limit=k)
            for fact in found:
                if fact.id in quarantined or fact.id in results:
                    continue
                results[fact.id] = fact
            if len(results) >= k:
                break
        ranked = sorted(
            results.values(), key=lambda f: (-f.confidence, -f.importance, f.id)
        )
        shaped: list[dict[str, Any]] = []
        for fact in ranked[:k]:
            shaped.append(await self._fact_dict(fact))
        return shaped

    async def _fact_dict(self, fact: Fact) -> dict[str, Any]:
        """One fact as the recall/bundle mapping, origin session resolved."""
        return {
            "id": fact.id,
            "statement": fact.statement,
            "confidence": fact.confidence,
            "importance": fact.importance,
            "memory_domain": fact.domain,
            "entity_name": fact.entity_name,
            "predicate": fact.predicate,
            "object_value": fact.object_value,
            "source_signal_id": fact.source_signal_id,
            "session_id": await self._origin_session(fact),
        }

    async def _origin_session(self, fact: Fact) -> str | None:
        """The chat session a fact came from, via its signal's external id.

        The signal's ``external_id`` is ``<session_id>:<turn_id>:<role>``
        (see :meth:`remember`), so the origin is one column read — no JSON
        parse, no derived index. A fact from outside chat has no chat signal
        and answers ``None``, which is the honest answer.
        """
        if self._signals is None or fact.source_signal_id is None:
            return None
        signal = await self._signals.get(fact.source_signal_id)
        if signal is None or signal.external_id is None:
            return None
        return signal.external_id.split(":", 1)[0] or None

    async def _quarantined_fact_ids(self) -> frozenset[str]:
        """Ids of facts parked in the open-conflict buffer (RFC §4.2).

        The buffer stores the two sides of a contradiction as JSON blobs;
        both carry the fact ``id``. Parsing is bounded by the open-conflict
        set, and an unreadable row contributes nothing rather than failing
        the recall — quarantine exclusion must not become a recall outage.
        """
        rows = await self._db.fetch_all(
            "SELECT incumbent_fact, challenger_fact FROM hot_serving_unresolved_buffer "
            "WHERE resolved_at IS NULL"
        )
        ids: set[str] = set()
        for row in rows:
            for column in ("incumbent_fact", "challenger_fact"):
                try:
                    parsed: Any = json.loads(row[column])
                except ValueError:
                    continue
                if isinstance(parsed, Mapping) and isinstance(parsed.get("id"), str):
                    ids.add(parsed["id"])
        return frozenset(ids)

    # -- determinism aids and heartbeat ---------------------------------------

    async def now(self) -> datetime:
        """Recorded time — the clock a replayable pipeline is allowed to read."""
        return datetime.now(UTC)

    async def random(self) -> float:
        """Seeded per session lineage, so a replayed session draws the same values."""
        self._rng_state = (
            self._rng_state * 6364136223846793005 + 1442695040888963407
        ) % (1 << 64)
        return self._rng_state / (1 << 64)

    async def progress(self, status: str) -> None:
        """Heartbeat; never journalled."""
        log.debug(
            "chat.progress",
            session_id=str(self._session_id),
            turn_id=self._turn_id,
            status=status,
        )

    # -- refused capabilities (protocol shape; the grant set refuses first) ----

    async def call_agent(
        self, role: AgentRole, message: AgentMessage, agent_ctx: AgentContext
    ) -> AgentResult[Any]:
        self._grants.require(Capability.CALL_AGENT)
        raise CapabilityNotGrantedError(Capability.CALL_AGENT.value)  # pragma: no cover

    async def use_skill(self, name: str, args: Mapping[str, Any]) -> str:
        self._grants.require(Capability.USE_SKILL)
        raise CapabilityNotGrantedError(Capability.USE_SKILL.value)  # pragma: no cover

    async def gate(self, role: AgentRole, payload: Mapping[str, Any]) -> TaskProjection:
        self._grants.require(Capability.GATE)
        raise CapabilityNotGrantedError(Capability.GATE.value)  # pragma: no cover

    async def submit_plan(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        requirements: Mapping[str, Any] | None = None,
        bypassed_planner: bool = False,
    ) -> None:
        self._grants.require(Capability.PLAN_SUBMIT)
        raise CapabilityNotGrantedError(Capability.PLAN_SUBMIT.value)  # pragma: no cover

    async def check_policy(self, state: TaskProjection) -> TaskProjection:
        self._grants.require(Capability.POLICY_CHECK)
        raise CapabilityNotGrantedError(Capability.POLICY_CHECK.value)  # pragma: no cover

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        self._grants.require(Capability.COMMIT)
        raise CapabilityNotGrantedError(Capability.COMMIT.value)  # pragma: no cover

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        self._grants.require(Capability.SUB_RUN)
        raise CapabilityNotGrantedError(Capability.SUB_RUN.value)  # pragma: no cover

    # -- in-process transport reads -------------------------------------------

    async def stage_question(self, role: AgentRole) -> GateQuestion | None:
        """Chat opens no gates; there is never a question staged."""
        return None

    async def project(self) -> TaskProjection:
        """Re-read the session lineage's projection (the R2 read)."""
        return await project(self._ledger, self._session_id)


def _recall_probes(query: str) -> list[str]:
    """The probe order for one recall: whole phrase first, then tokens.

    Deterministic: the full normalised query, then the length-3+ tokens
    sorted by descending length with an alphabetical tiebreak — never dict
    order, never wall-clock. NULs are dropped, not refused: this is an
    internal recall path (AGENTS.md's split — a model-supplied tool argument
    is refused, an internal path drops it and carries on).
    """
    normalised = " ".join(query.replace("\x00", " ").split())
    probes: list[str] = []
    if normalised:
        probes.append(normalised)
    tokens = {
        stripped
        for token in normalised.split()
        if len(stripped := token.strip(_PROBE_STRIP)) >= 3
    }
    probes.extend(sorted(tokens, key=lambda t: (-len(t), t)))
    return probes


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class ChatLoop:
    """Built-in pipeline #2: one conversational turn per ``run``.

    Constructed with the session journal (the host built it; the loop fires
    it at the flow points, exactly as the coder loop fires its
    :class:`~paa.coder.journal.LedgerToolJournal`). The facade the host
    minted carries the capabilities; the loop carries the order of
    operations. The class is stateless between runs and one instance may
    serve every turn of the session its journal is bound to.
    """

    name = "builtin.chat"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = CHAT_CAPABILITIES
    limits = RunLimits()

    def __init__(self, journal: ChatSessionJournal) -> None:
        self._journal = journal

    async def run(self, task: PipelineTask, ctx: _FacadeProtocol) -> PipelineResult:
        """Drive one turn: propose → recall → bundle → think → remember → complete."""
        facade = self._turn_facade(ctx)
        turn_id = _turn_id_of(task)
        text = task.goal

        # Ledger before side effect, always: the PROPOSED row attests the
        # user's message (hash, length, shape preview) before anything reads
        # memory, calls a model, or writes a signal.
        await self._journal.turn_proposed(turn_id, text)

        collected: list[str] = []
        user_delta = task.params.get("on_delta")
        sink: DeltaSink | None = None
        if facade.streaming and user_delta is not None:

            async def sink(delta: str) -> None:
                collected.append(delta)
                await user_delta(delta)

        try:
            recalled = await facade.recall(text, k=facade.recall_k)
            turns = await facade.episodic_turns(facade.session_id, k=facade.last_k_turns)
            # The context-provider slot (C6.4 / P1.5): the packet comes
            # from whatever the host resolved the slot to — the shipped
            # compiler by default, an overlay's replacement when the
            # operator filled the slot. The replacement owns the same
            # contract: same inputs, a bundle (or bundle-shaped) packet
            # back, and the packet — not the inputs — is what the prompt
            # renders below.
            bundle = facade.build_context(
                persona=facade.persona,
                recalled_facts=recalled,
                recent_turns=turns,
                token_ceiling=facade.tokens_budget,
            )
            # The prompt renders the packet, not the inputs: persona and
            # history are what the bundle packed (a ceiling that evicted the
            # persona assembled the packet without its instructions — the
            # prompt must say so, not sneak the instructions back in), and
            # the facts section is the provenance (packed facts only). The
            # user's message rides outside the budget: it is the input the
            # turn exists to answer, not assembled context.
            packed_persona = next(
                (
                    element.content
                    for element in bundle.packet.elements
                    if element.slot == PERSONA_SLOT
                ),
                "",
            )
            prompt = render_chat_prompt(
                persona=packed_persona,
                facts=[p.statement for p in bundle.provenance],
                turns=bundle.turns,
                message=text,
            )

            completion = await facade.think(
                prompt,
                budget=facade.tokens_budget,
                on_delta=sink,
            )
            reply = str(completion.get("text", ""))
            tokens_used = int(completion.get("tokens_used", 0))

            await facade.remember({"kind": _ROLE_USER, "text": text, "turn_id": turn_id})
            if reply:
                await facade.remember(
                    {"kind": _ROLE_ASSISTANT, "text": reply, "turn_id": turn_id}
                )
            writes = facade.writes
            user_write: dict[str, Any] = next(
                (w for w in writes if w.get("role") == _ROLE_USER),
                {"facts_written": []},
            )

            signal_ids = [
                str(write["signal_id"])
                for write in writes
                if write.get("signal_id") is not None
            ]
            recalled_ids = [str(fact["id"]) for fact in recalled]
            await self._journal.turn_completed(
                turn_id,
                reply=reply,
                tokens_consumed=tokens_used,
                recalled_fact_ids=recalled_ids,
                signal_ids=signal_ids,
                facts_written=[str(fid) for fid in user_write.get("facts_written", [])],
            )
            return PipelineResult(
                status=PipelineStatus.COMPLETED,
                summary=reply,
                metrics={
                    "turn_id": turn_id,
                    "recalled_fact_ids": recalled_ids,
                    "signal_ids": signal_ids,
                    "tokens_consumed": tokens_used,
                    "bundle_tokens": bundle.packet.allocated_tokens,
                    "token_ceiling": bundle.token_ceiling,
                },
                artifacts=[
                    PipelineArtifact(kind="chat_reply", sha256=text_sha256(reply))
                ],
            )
        except asyncio.CancelledError:
            # Shielded: the task is already unwinding, and a second cancel
            # mid-write must not drop the CANCELLED row and leave the
            # PROPOSED outcome-less (the sweep would then record a crash
            # where the operator stopped the turn). Re-raised, never
            # swallowed — a cancelled run is not a completed one.
            await asyncio.shield(
                self._journal.turn_cancelled(turn_id, partial_reply="".join(collected))
            )
            raise
        except Exception as exc:
            reason = redact(f"{type(exc).__name__}: {exc}")[:200]
            await self._journal.turn_failed(turn_id, reason=reason)
            return PipelineResult(
                status=PipelineStatus.FAILED,
                summary=reason,
                metrics={"turn_id": turn_id},
            )

    def _turn_facade(self, ctx: _FacadeProtocol) -> ChatTurnFacade:
        """Narrow the protocol to the facade the chat host mints.

        A foreign facade means the host wired the wrong door; that is a
        host bug worth naming, not a duck type to tolerate.
        """
        if not isinstance(ctx, ChatTurnFacade):
            raise PaaError(
                "chat pipeline requires the host-minted ChatTurnFacade",
                actual=type(ctx).__name__,
            )
        return ctx


def _turn_id_of(task: PipelineTask) -> str:
    """The turn id from the per-run task params, validated for the key."""
    value = task.params.get("turn_id")
    if not isinstance(value, str) or not value or ":" in value:
        raise ValueError(
            "chat turn requires a params['turn_id'] that is non-empty and free of ':' "
            f"— got {value!r}"
        )
    return value
