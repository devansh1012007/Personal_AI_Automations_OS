"""Bounded context assembly — the production form of the RFC §5.2 sketch.

This is the runtime's defence against context-window flooding (RFC §11.1). It
takes candidates that have *already* been retrieved from the hot store and the
vector index, ranks them, and packs as much decision-relevant signal as will
fit under a hard token ceiling — 1500 for the planner, 1000 for a worker.

The gatherer performs **no retrieval of its own**. It is handed candidates and
returns a packet. That keeps it pure, synchronous and exhaustively testable,
which matters because the ceiling it enforces is a Definition-of-Done item: for
any input whatsoever, ``sum(element tokens) <= ceiling`` must hold.

The RFC §5.2 pseudocode carries four defects that would each cause real damage
in production. All four are fixed here and each fix is documented at its site:

1. ``O(n²)`` deduplication (:meth:`BoundedContextGatherer._candidate_pool`)
2. non-deterministic tie ordering (:func:`_rank_key`)
3. ``break`` on the first oversized fact (:meth:`BoundedContextGatherer._pack`)
4. an unchecked append on the high-importance path (:meth:`_pack` again)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from paa.config import ContextSettings, get_settings
from paa.context.budget import CharEstimator, TokenBudget, TokenEstimator
from paa.context.metrics import (
    context_density,
    context_entropy,
    context_pollution_ratio,
    context_utility_score,
    token_efficiency,
)
from paa.core.errors import ContextInsufficientError

__all__ = [
    "NARRATIVE_MEMORY_DOMAINS",
    "BoundedContextGatherer",
    "ContextElement",
    "ContextPacket",
    "RoutingDirective",
]

log = structlog.get_logger(__name__)

RoutingDirective = Literal[
    "PROCEED_TO_PLANNER",
    "TRIGGER_BACKGROUND_HYDRATION",
    "HARD_STOP_ESCALATE_TO_USER",
]

#: Memory domains a **worker** packet must never carry.
#:
#: RFC §2.2 separates planner context from worker context, and the separation is
#: not merely a size optimisation. A worker executes a named step against files
#: and primitives; handing it the session narrative — what the user said last
#: Tuesday, how a past attempt felt, a distilled relationship summary — invites
#: it to re-litigate the plan it was given instead of executing it. Narrative is
#: the planner's input and the worker's distraction.
#:
#: The domain names track ``MemorySettings.decay_lambda`` plus the conventional
#: aliases retrieval layers emit:
#:
#: ``reflection``
#:     the runtime's own retrospection on past runs — planner-level input.
#: ``relationship``
#:     social/entity narrative from the graph, not file state.
#: ``long_term_distilled``
#:     compressed session narrative; distilled prose by construction.
#: ``narrative`` / ``episodic`` / ``conversation`` / ``conversational`` /
#: ``dialogue`` / ``summary``
#:     conventional aliases for the same content from different retrievers.
#:
#: Deliberately a *blocklist* rather than an allowlist: a retriever that invents
#: a new factual domain (``build_artifact``, say) should reach the worker
#: without needing this constant edited, whereas a new *narrative* domain is a
#: deliberate act by someone who can add it here.
NARRATIVE_MEMORY_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "conversation",
        "conversational",
        "dialogue",
        "episodic",
        "long_term_distilled",
        "narrative",
        "reflection",
        "relationship",
        "summary",
    }
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ContextElement(BaseModel):
    """One retrieved candidate considered for inclusion in a packet.

    Frozen: a packet is an audit record. If an element could be mutated after
    compilation, the recorded ``allocated_tokens`` and ``density`` would stop
    describing the thing that was actually sent to the model.

    Structurally satisfies :class:`~paa.context.metrics.ScoredFact`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    """Stable identity from the source substrate. Used for deduplication and,
    critically, as the final ordering tiebreak — so it must be unique per fact
    and stable across retrievals of the same fact."""

    content: str = ""
    """Rendered text as it will appear in the prompt. Token cost is measured
    from this unless ``token_cost`` overrides it."""

    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    """Retrieval score against the task. Cosine similarity for vector matches;
    hot facts default to 1.0 because they were selected by exact key."""

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    """The memory substrate's belief in this fact, post-decay (RFC §4.1)."""

    importance: float = Field(default=0.0, ge=0.0, le=1.0)
    """At or above ``ContextSettings.invariant_importance`` this fact is a
    system invariant and outranks ordinary candidates even when it resolves no
    required slot."""

    slot: str | None = None
    """The required slot this fact resolves, if any.

    One slot per element by design. A fact claiming to resolve several slots is
    really several facts, and modelling it as one would make ``density``
    ambiguous — a single 3-slot element could carry a task to
    ``PROCEED_TO_PLANNER`` on one retrieval that no reviewer could later
    decompose."""

    memory_domain: str = "semantic"
    """Source domain. Drives worker-packet filtering via
    :data:`NARRATIVE_MEMORY_DOMAINS` and decay-rate selection upstream."""

    token_cost: int | None = Field(default=None, ge=0)
    """Pre-computed token count. When ``None`` the gatherer's estimator measures
    ``content``. Set this when the caller already tokenized the text, so the
    packet is charged the true count rather than a heuristic."""

    def tokens(self, estimator: TokenEstimator) -> int:
        """Token cost of this element, preferring an explicit ``token_cost``."""
        if self.token_cost is not None:
            return self.token_cost
        return estimator.estimate(self.content)


class ContextPacket(BaseModel):
    """The bounded context handed to a planner or worker.

    Frozen and fully self-describing: every number needed to explain *why* the
    runtime routed a task the way it did is on this object, so the packet can be
    written straight into a ``CONTEXT_HYDRATED`` ledger payload and re-read
    months later without the gatherer being re-run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    elements: tuple[ContextElement, ...] = ()
    """Selected facts, in the priority order they were packed."""

    density: float = Field(ge=0.0, le=1.0)
    """RFC §5.1(3) slot-fill ratio. The routing variable."""

    allocated_tokens: int = Field(ge=0)
    """Total token cost of ``elements``. Guaranteed ``<=`` the ceiling."""

    routing_directive: RoutingDirective
    """What the orchestrator should do next."""

    vacant_slots: tuple[str, ...] = ()
    """Required slots no surviving element resolved, in declaration order."""

    pollution_ratio: float = Field(ge=0.0, le=1.0)
    """RFC §15.9. Share of packet tokens resolving no required slot."""

    utility_score: float = Field(ge=0.0)
    """RFC §15.1. Total decision-usable signal carried."""

    entropy: float = Field(ge=0.0)
    """RFC §15.4, bits. Spread of token mass across elements — high means
    attention is scattered over many small fragments."""

    token_ceiling: int = Field(ge=0)
    """The ceiling this packet was compiled against. Recorded so an audit can
    verify the bound without knowing which settings were live at the time."""

    @property
    def is_sufficient(self) -> bool:
        """Whether the planner may run on this packet unassisted."""
        return self.routing_directive == "PROCEED_TO_PLANNER"

    @property
    def filled_slots(self) -> tuple[str, ...]:
        """Required slots that were resolved, in packing order."""
        seen: set[str] = set()
        out: list[str] = []
        for element in self.elements:
            if element.slot is not None and element.slot not in seen:
                seen.add(element.slot)
                out.append(element.slot)
        return tuple(out)

    def efficiency(self) -> float:
        """RFC §5.1(5) density per 1000 tokens. See :func:`token_efficiency`."""
        return token_efficiency(self.density, self.allocated_tokens)

    def raise_if_insufficient(self) -> None:
        """Raise :class:`ContextInsufficientError` on a hard stop.

        Compilation itself never raises — a hard stop is a legitimate, recorded
        outcome and the orchestrator needs the packet's numbers to explain the
        escalation to the user. Callers that would rather fail fast opt in here.
        """
        if self.routing_directive == "HARD_STOP_ESCALATE_TO_USER":
            raise ContextInsufficientError(self.density, list(self.vacant_slots))


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

#: Ranking tiers, lowest sorts first.
_TIER_SLOT_RESOLVER: Final[int] = 0
_TIER_INVARIANT: Final[int] = 1
_TIER_SUPPORTING: Final[int] = 2


def _rank_key(
    element: ContextElement,
    required: frozenset[str],
    invariant_importance: float,
) -> tuple[int, float, float, str]:
    """Total ordering over candidates. Lower sorts first.

    Three tiers, then score, then a deterministic tiebreak:

    ``0`` resolves a required slot — these buy ``density``, which is the only
    thing that moves the routing directive.
    ``1`` a system invariant (``importance >= invariant_importance``) — the
    facts that must survive pruning even though they fill no slot, e.g. a
    standing user constraint that would make an otherwise-good plan wrong.
    ``2`` everything else, supporting colour.

    SPEC FIX — deterministic ties. The RFC §5.2 sketch sorts on
    ``(bool, float)``. The tiering is right, but ties on the float fall back to
    Python's stable sort, i.e. to whatever order the retrieval layer happened to
    emit — which for a vector index is not stable across runs, and for a set or
    dict iteration is not stable across processes. Two identical requests could
    then produce different packets, so a replayed correlation would diverge from
    its ledger and RFC §1.5's determinism guarantee would fail on a subsystem
    that looks pure. Ties are broken on ``id``, which is stable by contract, so
    the ordering is total and reproducible.
    """
    resolves_slot = element.slot is not None and element.slot in required
    if resolves_slot:
        tier = _TIER_SLOT_RESOLVER
    elif element.importance >= invariant_importance:
        tier = _TIER_INVARIANT
    else:
        tier = _TIER_SUPPORTING

    # Negated so that descending score/importance sorts ascending alongside the
    # ascending tier and id, giving one comparison direction for the whole key.
    return (tier, -(element.relevance * element.confidence), -element.importance, element.id)


# ---------------------------------------------------------------------------
# Gatherer
# ---------------------------------------------------------------------------


class BoundedContextGatherer:
    """Compiles bounded context packets. Stateless and reusable.

    One instance may serve many tasks concurrently: :meth:`compile` holds no
    state between calls and mutates nothing on ``self``.
    """

    def __init__(
        self,
        settings: ContextSettings | None = None,
        *,
        estimator: TokenEstimator | None = None,
    ) -> None:
        """
        :param settings: context tuning. Defaults to the process settings.
        :param estimator: token measurement. Defaults to :class:`CharEstimator`
            built from ``settings``.

            Note this default is *not* :func:`~paa.context.budget.default_estimator`,
            which would opportunistically pick up ``tiktoken`` when installed.
            The ceiling must be enforced identically on every machine, and a
            packet whose contents depend on which optional packages a developer
            happens to have would break cross-machine ledger replay. Callers
            wanting exact BPE counts pass an estimator explicitly and accept
            that trade.
        """
        self.settings = settings if settings is not None else get_settings().context
        self.estimator = (
            estimator if estimator is not None else CharEstimator.from_settings(self.settings)
        )

    # -- public API --------------------------------------------------------

    def compile(
        self,
        hot_facts: Iterable[ContextElement],
        semantic_matches: Iterable[ContextElement],
        required_slots: Sequence[str],
        *,
        token_ceiling: int | None = None,
    ) -> ContextPacket:
        """Assemble a planner packet under the RFC §5 token ceiling.

        :param hot_facts: candidates from the relational hot store. Filtered by
            ``confidence_floor``.
        :param semantic_matches: candidates from the vector index. Filtered by
            ``relevance_floor``. Deduplicated against ``hot_facts``, which win
            on collision — the relational record is the authoritative one, the
            vector hit is a pointer to it.
        :param required_slots: information the task declared it needs.
            Duplicates are collapsed; declaration order is preserved in
            ``vacant_slots``.
        :param token_ceiling: overrides ``ContextSettings.token_ceiling``.
        :returns: a :class:`ContextPacket` whose ``allocated_tokens`` never
            exceeds the ceiling.
        """
        ceiling = self._resolve_ceiling(token_ceiling, self.settings.token_ceiling)
        return self._compile(
            hot_facts,
            semantic_matches,
            required_slots,
            ceiling=ceiling,
            blocked_domains=frozenset(),
            packet_kind="planner",
        )

    def compile_worker_packet(
        self,
        hot_facts: Iterable[ContextElement],
        semantic_matches: Iterable[ContextElement],
        required_slots: Sequence[str],
        *,
        token_ceiling: int | None = None,
    ) -> ContextPacket:
        """Assemble a worker packet: tighter ceiling, no narrative.

        Two differences from :meth:`compile`, both from RFC §2.2's planner/worker
        context separation:

        * the ceiling defaults to ``worker_token_ceiling`` (1000, not 1500);
        * every candidate whose ``memory_domain`` is in
          :data:`NARRATIVE_MEMORY_DOMAINS` is dropped before ranking.

        The filter runs *before* ranking rather than after packing, so a
        narrative fact can never displace a factual one it outranked and then be
        removed, leaving a hole the packer has already moved past.

        Note that a narrative element is dropped **even when it resolves a
        required slot**. That is intentional: if a slot can only be filled by
        conversational recall, the worker is being asked to make a judgement
        that belongs to the planner, and the resulting vacancy correctly drives
        the density down and the packet toward hydration or escalation rather
        than quietly smuggling prose into an executor.
        """
        ceiling = self._resolve_ceiling(token_ceiling, self.settings.worker_token_ceiling)
        return self._compile(
            hot_facts,
            semantic_matches,
            required_slots,
            ceiling=ceiling,
            blocked_domains=NARRATIVE_MEMORY_DOMAINS,
            packet_kind="worker",
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _resolve_ceiling(override: int | None, default: int) -> int:
        if override is None:
            return default
        if isinstance(override, bool) or not isinstance(override, int):
            raise ValueError(f"token_ceiling must be an int, got {type(override).__name__}")
        if override < 0:
            raise ValueError(f"token_ceiling must be non-negative, got {override}")
        return override

    def _candidate_pool(
        self,
        hot_facts: Iterable[ContextElement],
        semantic_matches: Iterable[ContextElement],
        blocked_domains: frozenset[str],
    ) -> list[ContextElement]:
        """Filter by quality floors and deduplicate by id.

        SPEC FIX — quadratic dedupe. The RFC §5.2 sketch deduplicates with a
        list comprehension that rescans the accumulated pool for every match,
        i.e. ``O(n²)`` comparisons. With a graph fan-out of 3 hops the candidate
        pool routinely reaches the low thousands, and this sits on the hot path
        of every single task. A ``set`` of seen ids makes it ``O(n)`` with no
        behavioural change.

        Hot facts are admitted first so they win id collisions: both substrates
        can surface the same underlying record, and the relational row carries
        the authoritative confidence while the vector hit carries only a
        similarity.

        The quality floors apply uniformly, including to slot-resolving facts. A
        barely-believed fact that fills a slot is worse than an unfilled slot:
        the unfilled slot routes the task to hydration or a human, whereas the
        weak fact silently satisfies ``density`` and the planner proceeds on
        something the memory layer does not actually stand behind.
        """
        seen: set[str] = set()
        pool: list[ContextElement] = []

        for element in hot_facts:
            if element.memory_domain in blocked_domains:
                continue
            if element.confidence < self.settings.confidence_floor:
                continue
            if element.id in seen:
                continue
            seen.add(element.id)
            pool.append(element)

        for element in semantic_matches:
            if element.memory_domain in blocked_domains:
                continue
            if element.relevance < self.settings.relevance_floor:
                continue
            if element.id in seen:
                continue
            seen.add(element.id)
            pool.append(element)

        return pool

    def _pack(
        self,
        ranked: list[ContextElement],
        ceiling: int,
    ) -> tuple[list[ContextElement], list[int]]:
        """Greedily fill the budget in priority order.

        SPEC FIX — packing efficiency. The RFC §5.2 sketch ``break``s out of the
        loop on the first fact that does not fit. Because the list is sorted by
        priority, the oversized fact is *likely to be a high-priority one* — a
        pasted stack trace or a whole file attached to the most relevant slot —
        so a single fat candidate truncates the packet exactly when the context
        matters most, discarding every smaller fact behind it that would have
        fit comfortably. Switching to ``continue`` skips only the item that does
        not fit and keeps packing.

        SPEC FIX — unchecked append. The sketch's high-importance branch appends
        invariants without consulting the ceiling at all, which is a direct
        breach of the DoD bound: enough invariants and the packet exceeds 1500
        tokens with no error raised anywhere. Here there is exactly **one**
        append site and it is unconditionally gated on
        :meth:`TokenBudget.try_consume`. Invariants get *priority* through the
        ranking tier, which is what "survive pruning" should have meant; they do
        not get an exemption from the bound. A ceiling that some facts may
        ignore is not a ceiling.

        This is greedy-by-priority, not an optimal knapsack solution. That is a
        deliberate trade: the optimal packing is NP-hard, and its objective
        (maximise tokens used) is the wrong one anyway — the packet exists to
        carry the *highest-priority* facts, not the combination that happens to
        fill the budget most snugly.
        """
        budget = TokenBudget(ceiling, kind="context_tokens")
        selected: list[ContextElement] = []
        costs: list[int] = []

        for element in ranked:
            cost = element.tokens(self.estimator)
            if not budget.try_consume(cost):
                continue
            selected.append(element)
            costs.append(cost)

        return selected, costs

    def _compile(
        self,
        hot_facts: Iterable[ContextElement],
        semantic_matches: Iterable[ContextElement],
        required_slots: Sequence[str],
        *,
        ceiling: int,
        blocked_domains: frozenset[str],
        packet_kind: str,
    ) -> ContextPacket:
        """Shared compilation pipeline for planner and worker packets."""
        # Declaration order preserved, duplicates collapsed: vacant_slots reads
        # back in the order the caller declared its requirements.
        ordered_slots = list(dict.fromkeys(required_slots))
        required = frozenset(ordered_slots)

        pool = self._candidate_pool(hot_facts, semantic_matches, blocked_domains)
        invariant_floor = self.settings.invariant_importance
        pool.sort(key=lambda element: _rank_key(element, required, invariant_floor))

        selected, costs = self._pack(pool, ceiling)

        allocated = sum(costs)
        resolved = {
            element.slot
            for element in selected
            if element.slot is not None and element.slot in required
        }
        vacant = tuple(slot for slot in ordered_slots if slot not in resolved)

        density = context_density(len(resolved), len(ordered_slots))

        # Pollution is measured as the share of packet tokens that resolve no
        # required slot. Invariants and supporting colour therefore *count* as
        # pollution, which is correct: they are carried on judgement, not on
        # demonstrated need, and the metric exists to make that cost visible.
        unreferenced = sum(
            cost
            for element, cost in zip(selected, costs, strict=True)
            if element.slot is None or element.slot not in required
        )
        pollution = context_pollution_ratio(unreferenced, allocated)
        utility = context_utility_score(selected, pollution)

        # Entropy over each element's share of the packet's token mass: a packet
        # dominated by one large fact is low-entropy/focused, one spread thinly
        # over many fragments is high-entropy/scattered.
        entropy = context_entropy([cost / allocated for cost in costs] if allocated else [])

        directive = self._route(density)

        packet = ContextPacket(
            elements=tuple(selected),
            density=density,
            allocated_tokens=allocated,
            routing_directive=directive,
            vacant_slots=vacant,
            pollution_ratio=pollution,
            utility_score=utility,
            entropy=entropy,
            token_ceiling=ceiling,
        )

        # Belt-and-braces on the DoD bound. Everything above should make this
        # unreachable; if a future refactor introduces an un-gated append, this
        # fails loudly at the boundary instead of silently shipping an
        # oversized prompt to the model.
        if packet.allocated_tokens > ceiling:  # pragma: no cover — defensive
            raise AssertionError(
                f"context packet breached its ceiling: "
                f"{packet.allocated_tokens} > {ceiling}"
            )

        log.debug(
            "context_packet_compiled",
            kind=packet_kind,
            candidates=len(pool),
            selected=len(selected),
            allocated_tokens=allocated,
            ceiling=ceiling,
            density=round(density, 4),
            routing_directive=directive,
            vacant_slots=len(vacant),
        )
        return packet

    def _route(self, density: float) -> RoutingDirective:
        """Map density onto the RFC §5.2 routing directive.

        Both comparisons are ``>=`` so a density landing exactly on a configured
        threshold takes the more permissive branch, matching how the thresholds
        read in ``ContextSettings`` ("at or above which the planner may run").
        """
        if density >= self.settings.density_proceed:
            return "PROCEED_TO_PLANNER"
        if density >= self.settings.density_hydrate:
            return "TRIGGER_BACKGROUND_HYDRATION"
        return "HARD_STOP_ESCALATE_TO_USER"
