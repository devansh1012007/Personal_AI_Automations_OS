"""The two context builders. RFC §2.1 agents 2 and 5, §2.2.

Why two and not one
-------------------
RFC §2.2 argues the separation on token cost and focus. There is a stronger
reason: **blast radius**. The planner packet legitimately contains strategy,
themes and relationship history. A worker runs semi-trusted code inside a
sandbox. Handing the worker the planner's packet would place the user's
long-horizon thinking inside the process most likely to be compromised.

So the split is a security boundary, not an optimisation, and
:class:`WorkerContextBuilder` enforces it by dropping every narrative domain
outright rather than trusting a prompt to stay on topic.

On "context builder uses AI"
----------------------------
The project brief asks for this, and it is implemented — but with a strict
division of labour:

* **AI proposes what to look for.** Query expansion and slot inference: turning
  "fix the login bug" into search terms and a required-slot list.
* **Deterministic code decides what gets in.** Ranking, the token ceiling, and
  the routing directive stay in :class:`~paa.context.gatherer.BoundedContextGatherer`.

That boundary matters. If a model chose packet contents, then text retrieved
from an untrusted source could argue its way into the planner's context — and
the packet would stop being reproducible, breaking ledger replay. With the
split, the worst a compromised expansion can do is retrieve *irrelevant* facts,
which the density check then catches.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.context.gatherer import BoundedContextGatherer, ContextElement
from paa.core.types import AgentRole
from paa.memory.decay import effective_confidence
from paa.memory.domains import narrative_domains

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = ["PlannerContextBuilder", "QueryExpander", "WorkerContextBuilder"]

log = structlog.get_logger(__name__)

#: Words carrying no retrieval signal. Dropped before a goal becomes search
#: terms, so "fix the login bug" queries on {fix, login, bug} rather than
#: matching every fact containing "the".
# A word list, kept as split text rather than a 50-element literal: the literal
# form is either one line over the length limit or an unreadable multi-line
# block. SIM905 is ignored for this file in pyproject for that reason.
_STOPWORDS = frozenset(
    (
        "a an and are as at be but by for from has have how i if in into is it its of "
        "on or that the their then there these this to was were what when where which "
        "who will with would you your please can could should make made get got need "
        "want"
    ).split()
)


class QueryExpander(Protocol):
    """Optional AI assist: goal text -> search terms and required slots."""

    async def expand(self, goal: str) -> tuple[list[str], list[str]]:
        """Return ``(search_terms, required_slots)``."""
        ...


class _KeywordExpander:
    """Deterministic fallback. No model, no network, fully reproducible."""

    async def expand(self, goal: str) -> tuple[list[str], list[str]]:
        words = re.findall(r"[a-zA-Z_][\w\-.]{2,}", goal.lower())
        terms = [w for w in dict.fromkeys(words) if w not in _STOPWORDS]
        # Anything that looks like a path or dotted identifier is very likely a
        # concrete artifact the task needs, so it becomes a required slot.
        slots = [t for t in terms if "." in t or "/" in t or "_" in t]
        return terms[:12], slots[:6]


class _BaseContextBuilder(Agent):
    """Shared retrieval plumbing.

    **Read-only by construction** (RFC §2.1 agent 2: "banned from writing to
    any storage layer"). Nothing in this class or its subclasses issues an
    INSERT, UPDATE or DELETE, and the only ``Database`` calls made are
    ``fetch_*``.
    """

    can_delegate = False

    def __init__(
        self,
        *,
        db: Database | None = None,
        vector_store: Any = None,
        graph_store: Any = None,
        embedder: Any = None,
        expander: QueryExpander | None = None,
        gatherer: BoundedContextGatherer | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._db = db
        self._vectors = vector_store
        self._graph = graph_store
        self._embedder = embedder
        self._expander = expander or _KeywordExpander()
        self._gatherer = gatherer or BoundedContextGatherer()

    async def _expand(self, goal: str, declared: list[str]) -> tuple[list[str], list[str]]:
        """Derive search terms and slots, tolerating an AI expander failure."""
        try:
            terms, inferred = await self._expander.expand(goal)
        except Exception as exc:
            # An expander outage must degrade retrieval quality, not fail the
            # task — hence the deterministic fallback is always available.
            log.warning("context.expander_failed", error=str(exc))
            terms, inferred = await _KeywordExpander().expand(goal)

        slots = list(dict.fromkeys([*declared, *inferred]))
        return terms, slots

    async def _hot_facts(self, terms: list[str], *, limit: int = 60) -> list[ContextElement]:
        """Relational candidates, with confidence decayed at read time.

        Decay is applied here rather than trusted from the row because the
        stored value is ``C₀``; using it directly would treat a two-year-stale
        fact as freshly verified.
        """
        if self._db is None or not terms:
            return []

        clauses = " OR ".join(["f.object_value LIKE ? OR f.predicate LIKE ?"] * len(terms))
        params: list[Any] = []
        for term in terms:
            params.extend([f"%{term}%", f"%{term}%"])
        params.append(limit)

        rows = await self._db.fetch_all(
            "SELECT f.id, f.entity_id, f.predicate, f.object_value, f.memory_domain, "
            "       f.initial_confidence, f.importance, f.last_queried_at, e.canonical_name "
            "FROM hot_serving_active_facts f "
            "JOIN hot_serving_entity_index e ON e.id = f.entity_id "
            f"WHERE f.superseded_by IS NULL AND ({clauses}) "
            "ORDER BY f.importance DESC LIMIT ?",
            params,
        )

        from paa.storage.relational.database import from_iso

        elements: list[ContextElement] = []
        for row in rows:
            try:
                confidence = effective_confidence(
                    row["initial_confidence"],
                    from_iso(row["last_queried_at"]),
                    row["memory_domain"],
                )
            except KeyError:
                confidence = row["initial_confidence"]
            elements.append(
                ContextElement(
                    id=row["id"],
                    content=f"{row['canonical_name']} {row['predicate']} {row['object_value']}",
                    relevance=1.0,  # exact-key retrieval
                    confidence=confidence,
                    importance=row["importance"],
                    slot=row["predicate"],
                    memory_domain=row["memory_domain"],
                )
            )
        return elements

    async def _semantic(self, goal: str, *, limit: int = 30) -> list[ContextElement]:
        if self._vectors is None or self._embedder is None:
            return []
        try:
            vector = (await self._embedder.embed([goal]))[0]
            hits = await self._vectors.search("active_facts", vector, limit=limit)
        except Exception as exc:
            log.warning("context.vector_search_failed", error=str(exc))
            return []

        return [
            ContextElement(
                id=str(hit.id),
                content=str(hit.payload.get("object_value", "")),
                relevance=float(hit.score),
                confidence=float(hit.payload.get("confidence", 0.8)),
                importance=float(hit.payload.get("importance", 0.5)),
                slot=hit.payload.get("predicate"),
                memory_domain=str(hit.payload.get("memory_domain", "semantic")),
            )
            for hit in hits
        ]

    async def _graph_neighbours(
        self, entity_ids: list[str], ctx: AgentContext
    ) -> list[ContextElement]:
        """Traverse provenance, bounded by the modality's hop budget.

        ``graph_hops`` is 0 for SIMPLE, so this returns immediately there —
        the traversal is skipped, not merely truncated.
        """
        hops = ctx.profile.graph_hops
        if self._graph is None or hops <= 0 or not entity_ids:
            return []

        elements: list[ContextElement] = []
        try:
            for entity_id in entity_ids[:5]:
                for path in await self._graph.traverse(entity_id, max_hops=hops):
                    elements.append(
                        ContextElement(
                            id=f"graph:{entity_id}:{len(elements)}",
                            content=str(path),
                            relevance=0.8,
                            confidence=0.9,
                            importance=0.4,
                            memory_domain="relationship",
                        )
                    )
        except Exception as exc:
            log.warning("context.graph_traversal_failed", error=str(exc))
        return elements


class PlannerContextBuilder(_BaseContextBuilder):
    """Strategic context for the planner. RFC §2.1 agent 2."""

    role = AgentRole.CONTEXT_BUILDER_PLANNER

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        goal = message.payload.get("goal", "")
        declared = list(message.payload.get("required_slots", []))
        terms, slots = await self._expand(goal, declared)

        hot = await self._hot_facts(terms)
        semantic = await self._semantic(goal)
        graph = await self._graph_neighbours([e.id for e in hot[:5]], ctx)

        packet = self._gatherer.compile(
            hot_facts=hot,
            semantic_matches=[*semantic, *graph],
            required_slots=slots,
            token_ceiling=min(ctx.profile.token_ceiling or 1500, 1500),
        )

        log.debug(
            "context.planner_packet",
            correlation_id=str(ctx.correlation_id),
            density=packet.density,
            tokens=packet.allocated_tokens,
            routing=packet.routing_directive,
        )
        return AgentResult.success(
            packet.model_dump(mode="json"),
            tokens_consumed=packet.allocated_tokens,
            confidence=packet.density,
        )


class WorkerContextBuilder(_BaseContextBuilder):
    """Execution context for a sandbox worker. RFC §2.1 agent 5.

    Two hard rules beyond the planner builder:

    1. **No narrative.** Every element from a narrative domain is dropped, so
       strategy and identity never enter a sandbox.
    2. **No holes.** A missing required primitive halts with
       ``hydration_required`` rather than dispatching a worker that will fail
       on a blank API host halfway through a mutation.
    """

    role = AgentRole.CONTEXT_BUILDER_WORKER

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        step = message.payload.get("step", {})
        required: list[str] = list(step.get("requires", []))
        goal = str(step.get("action", ""))

        terms, _ = await self._expand(goal, required)
        hot = await self._hot_facts(terms)

        blocked = narrative_domains()
        hot = [e for e in hot if e.memory_domain not in blocked]

        packet = self._gatherer.compile_worker_packet(
            hot_facts=hot,
            semantic_matches=[],
            required_slots=required,
            token_ceiling=ctx.profile.token_ceiling or 1000,
        )

        if packet.vacant_slots:
            log.info(
                "context.worker_hydration_required",
                correlation_id=str(ctx.correlation_id),
                vacant=list(packet.vacant_slots),
            )
            return AgentResult.failure(
                f"missing required primitives: {list(packet.vacant_slots)}",
                telemetry={
                    "hydration_required": True,
                    "vacant_slots": list(packet.vacant_slots),
                },
            )

        assert all(e.memory_domain not in blocked for e in packet.elements), (
            "narrative memory leaked into a worker packet"
        )
        return AgentResult.success(
            packet.model_dump(mode="json"),
            tokens_consumed=packet.allocated_tokens,
            confidence=packet.density,
        )
