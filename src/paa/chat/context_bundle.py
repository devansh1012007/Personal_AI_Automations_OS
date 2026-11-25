"""The chat context bundle: persona + recalled facts + this session's turns.

A thin, chat-facing wrapper over :class:`~paa.context.gatherer.
BoundedContextGatherer` (C4.3). The gatherer already owns the two properties
chat needs — a hard token ceiling enforced by construction, and a total,
deterministic ordering (``_rank_key`` sorts by tier, score, then the
element's stable id) — so this module adds only the chat *shape*: the
persona slot, the last-k turns of the session, and the provenance list the
TUI's memory rail will render. It performs no retrieval of its own and no
I/O; the caller hands it candidates and gets a bundle back, exactly as the
gatherer does.

Determinism is a contract, not a hope. Same inputs in, byte-identical
``packet`` JSON out — the golden tests pin it — and the ordering never
depends on dict iteration or wall-clock: element ids are position-indexed
for turns (``turn:000000``) and content-derived for facts (``fact:<id>``),
which is what makes the rank key total over equal-scored candidates.

Quarantine is a filter, not a tier. The memory layer's quarantine is
``hot_serving_unresolved_buffer`` (RFC §4.2: contradictory facts wait for a
human; neither record is believed while it waits). A quarantined fact's
confidence has already been degraded below every recall floor, so the
gatherer's own floor would usually drop it — but "usually" is not a
boundary. A fact mapping that carries a truthy ``quarantined`` marker is
excluded *before* ranking, so a caller that raises the floors cannot smuggle
a quarantined fact into a packet, and a caller that serves already-filtered
facts pays nothing. The chat facade stamps the marker from the open-conflict
buffer; the bundle trusts the marker and stays pure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from paa.config import ContextSettings, get_settings
from paa.context.budget import CharEstimator, TokenEstimator
from paa.context.gatherer import BoundedContextGatherer, ContextElement, ContextPacket

__all__ = [
    "PERSONA_SLOT",
    "BundleProvenance",
    "ChatContextBundle",
    "build_context_bundle",
]

#: The bundle's one required slot. The persona fills it, which is what makes
#: ``vacant_slots`` (and therefore the packet's density and routing
#: directive) say something true about the bundle: a packet whose persona did
#: not fit its ceiling is a packet assembled without its instructions.
PERSONA_SLOT: Final = "persona"

#: Element-id prefixes. They are the provenance join between a packet's
#: packed elements and the candidates they came from; changing one changes
#: the bundle's golden bytes.
_PERSONA_ID: Final = "chat:persona"
_FACT_ID_PREFIX: Final = "fact:"
_TURN_ID_PREFIX: Final = "turn:"

#: The turn element's domain. The gatherer's ``compile`` (unlike its worker
#: packet) blocks no domains, so narrative/episodic content is carried — a
#: chat packet without the conversation is not a chat packet.
_TURN_DOMAIN: Final = "episodic"
_FACT_DOMAIN_DEFAULT: Final = "semantic"


@dataclass(frozen=True, slots=True)
class BundleProvenance:
    """One consulted fact, as the memory rail renders it.

    ``origin_session`` is the chat session the fact came from (stamped onto
    the fact's signal when the turn was remembered) — the cross-session
    visibility that the isolation policy allows deliberately, made visible
    at the render surface rather than silently mixed in.
    """

    fact_id: str
    statement: str
    confidence: float
    importance: float
    origin_session: str | None


@dataclass(frozen=True, slots=True)
class ChatContextBundle:
    """The assembled turn context, and the record of what went into it."""

    packet: ContextPacket
    provenance: tuple[BundleProvenance, ...]
    """The facts that survived packing, in packet order."""

    turns: tuple[str, ...]
    """The rendered recent-turn lines the packet carries, oldest first.

    Carried, not merely handed in: under a tight ceiling the packer may
    drop turn lines, and the bundle reports what it packed — the same
    discipline :attr:`provenance` applies to facts. What the model sees
    under ``[history]`` is exactly this tuple, so a dropped line is a
    line the reply cannot quote."""

    persona: str
    token_ceiling: int

    def rendered_json(self) -> str:
        """The packet's canonical JSON — the golden-determinism surface.

        ``model_dump_json`` on a frozen pydantic model of primitives is a
        pure function of the values, so two bundles built from the same
        inputs are byte-identical here. This is what the golden test pins
        and what ``/context`` (the TUI's show-the-packet-verbatim command)
        will render.
        """
        return self.packet.model_dump_json()


def _statement_of(fact: Mapping[str, Any]) -> str:
    """The fact's rendered text: a ``statement`` if given, else composed."""
    statement = fact.get("statement")
    if isinstance(statement, str) and statement:
        return statement
    parts = [fact.get("entity_name"), fact.get("predicate"), fact.get("object_value")]
    composed = " ".join(str(p) for p in parts if p is not None)
    return composed or str(fact.get("id", ""))


def _fact_element(fact: Mapping[str, Any]) -> ContextElement | None:
    """One recalled fact as a candidate. ``None`` for a malformed mapping.

    A fact without a usable id cannot be deduplicated or tie-broken (the
    rank key's final term is the id), so it is refused rather than packed
    with an invented identity.
    """
    fact_id = fact.get("id")
    if not isinstance(fact_id, str) or not fact_id:
        return None
    return ContextElement(
        id=f"{_FACT_ID_PREFIX}{fact_id}",
        content=_statement_of(fact),
        relevance=1.0,
        confidence=_unit_float(fact.get("confidence"), 1.0),
        importance=_unit_float(fact.get("importance"), 0.0),
        memory_domain=str(fact.get("memory_domain") or _FACT_DOMAIN_DEFAULT),
    )


def _turn_element(index: int, turn: Mapping[str, Any]) -> ContextElement | None:
    """One recent turn as a candidate. Position-indexed id, stable rank key."""
    text = turn.get("text")
    if not isinstance(text, str) or not text:
        return None
    role = turn.get("role")
    role_text = role if isinstance(role, str) and role else "user"
    return ContextElement(
        id=f"{_TURN_ID_PREFIX}{index:06d}",
        content=f"{role_text}: {text}",
        relevance=1.0,
        confidence=1.0,
        importance=0.0,
        memory_domain=_TURN_DOMAIN,
    )


def _persona_element(persona: str) -> ContextElement | None:
    if not persona:
        return None
    return ContextElement(
        id=_PERSONA_ID,
        content=persona,
        relevance=1.0,
        confidence=1.0,
        importance=1.0,
        slot=PERSONA_SLOT,
    )


def _unit_float(value: Any, default: float) -> float:
    """A [0, 1] float from a candidate mapping, or the default.

    The gatherer validates its own fields (``ge``/``le`` on the model), so
    an out-of-range value here would raise at element construction; clamping
    keeps the bundle total over a recall result that carried, say, an
    unnormalised score.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number != number:  # NaN — a float can arrive from any substrate
        return default
    return min(1.0, max(0.0, number))


def _resolve_ceiling(
    token_ceiling: int | None, settings: ContextSettings | None
) -> int:
    """The bundle's ceiling: the explicit value, else the context engine's.

    Chat's default budget *is* the context engine's planner ceiling — one
    budget line for the runtime, not a second one that drifts. The
    ``ChatSettings.token_budget`` override is resolved by the caller (the
    loop) and arrives here as an explicit int.
    """
    if token_ceiling is not None:
        return token_ceiling
    effective = settings if settings is not None else get_settings().context
    return effective.token_ceiling


def build_context_bundle(
    *,
    persona: str,
    recalled_facts: Iterable[Mapping[str, Any]],
    recent_turns: Sequence[Mapping[str, Any]],
    token_ceiling: int | None = None,
    settings: ContextSettings | None = None,
    estimator: TokenEstimator | None = None,
) -> ChatContextBundle:
    """Assemble one turn's bundle under the token ceiling.

    :param persona: the persona slot's text; the first candidate offered and
        the slot the packet's density accounting tracks.
    :param recalled_facts: semantic facts from this turn's recall, as
        mappings with ``id`` plus the usual fact fields; a truthy
        ``quarantined`` key excludes the fact before ranking (see the module
        docstring).
    :param recent_turns: this session's last-k turns, oldest first, as
        ``{"role": ..., "text": ...}`` mappings.
    :param token_ceiling: explicit ceiling; ``None`` resolves the context
        engine's planner ceiling.
    :param settings: context tuning; defaults to the process settings.
    :param estimator: token measurement; defaults to the deterministic
        :class:`CharEstimator` (never the opportunistic tiktoken default —
        a bundle whose bytes depend on which optional package is installed
        would break the golden determinism this module promises).
    """
    gatherer_settings = settings if settings is not None else get_settings().context
    ceiling = _resolve_ceiling(token_ceiling, gatherer_settings)
    measure = estimator if estimator is not None else CharEstimator.from_settings(
        gatherer_settings
    )

    # The gatherer's quality floors are the task pipeline's policy, not chat's:
    # recall already decided what memory stands behind (ranked by confidence,
    # quarantine excluded), and the consult row this turn's facade wrote says
    # "served" — a packet that then silently dropped the fact would make the
    # ledger lie about what the model saw. The bundle keeps the gatherer's
    # ceiling and total ordering; it takes over the floor decision by zeroing
    # both floors on a private copy. The quarantine marker still excludes
    # before ranking, which is exactly why it exists (see the module docstring).
    floorless = gatherer_settings.model_copy(
        update={"confidence_floor": 0.0, "relevance_floor": 0.0}
    )

    persona_element = _persona_element(persona)

    kept_facts: list[Mapping[str, Any]] = []
    fact_elements: list[ContextElement] = []
    for fact in recalled_facts:
        if fact.get("quarantined"):
            continue
        element = _fact_element(fact)
        if element is None:
            continue
        kept_facts.append(fact)
        fact_elements.append(element)

    turn_elements: list[ContextElement] = []
    for index, turn in enumerate(recent_turns):
        element = _turn_element(index, turn)
        if element is None:
            continue
        turn_elements.append(element)

    hot: list[ContextElement] = (
        ([persona_element] if persona_element is not None else []) + fact_elements
    )
    packet = BoundedContextGatherer(floorless, estimator=measure).compile(
        hot,
        turn_elements,
        [PERSONA_SLOT],
        token_ceiling=ceiling,
    )

    by_element_id = {
        f"{_FACT_ID_PREFIX}{fact.get('id')}": fact
        for fact in kept_facts
        if isinstance(fact.get("id"), str)
    }
    provenance: list[BundleProvenance] = []
    for element in packet.elements:
        if not element.id.startswith(_FACT_ID_PREFIX):
            continue
        matched = by_element_id.get(element.id)
        if matched is None:  # pragma: no cover — the join is by construction
            continue
        origin = matched.get("session_id")
        provenance.append(
            BundleProvenance(
                fact_id=str(matched.get("id")),
                statement=element.content,
                confidence=element.confidence,
                importance=element.importance,
                origin_session=str(origin) if origin is not None else None,
            )
        )

    # The turn lines the packet actually carries, in conversation order. The
    # rank key ties every turn element on score and importance, so the id's
    # position index decides and the sort is total; sorting explicitly (rather
    # than trusting packet order) keeps the [history] section oldest-first even
    # if a re-ranker ever interleaves the elements.
    packed_turns = [
        element.content
        for element in sorted(
            (e for e in packet.elements if e.id.startswith(_TURN_ID_PREFIX)),
            key=lambda e: e.id,
        )
    ]

    return ChatContextBundle(
        packet=packet,
        provenance=tuple(provenance),
        turns=tuple(packed_turns),
        persona=persona,
        token_ceiling=ceiling,
    )
