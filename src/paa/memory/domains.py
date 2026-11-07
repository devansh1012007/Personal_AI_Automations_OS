"""The memory domain taxonomy.

RFC §4.1 enumerates 18 "distinct logical memory domains". Read carefully, most
of those rows are not distinct *mechanisms* — they are distinct *labels* over
the same handful of storage and lifecycle behaviours. For example "Skill
Memory" and "Procedural Memory" are both described as
``hot_serving.skill_registry`` with decay 0.00 and identifier lookup; they are
the same row twice. "Task Memory", "Session Memory" and "Working Memory" are
all volatile state keyed by an id with no decay.

SPEC DEVIATION (docs/adr/0014): we keep all 18 domain *names*, because they are
useful vocabulary and the RFC's retrieval-protocol column is meaningful. But
each maps onto one of six real **mechanisms**, and it is the mechanism that
determines the code path. This means adding a domain is a data change, not a
new subsystem — which is what the "modular and upgradeable" requirement needs.

The alternative — 18 bespoke pipelines — would be 18 places to fix every bug.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import ClassVar, Final

__all__ = [
    "DOMAINS",
    "DomainPolicy",
    "MemoryDomain",
    "MemoryMechanism",
    "RetrievalProtocol",
    "TierPolicy",
    "domain_policy",
    "narrative_domains",
]


class MemoryMechanism(str, enum.Enum):
    """How a domain is physically stored and maintained.

    Six mechanisms cover all 18 domains. Each has exactly one implementation.
    """

    VOLATILE = "volatile"
    """Process RAM or a TTL cache. Vanishes on restart, by design."""

    RELATIONAL = "relational"
    """Rows in hot serving. ACID, exact lookup, decay-eligible."""

    IMMUTABLE_LOG = "immutable_log"
    """Append-only cold lake. Never deleted, never decayed."""

    VECTOR = "vector"
    """Embedding index. Approximate recall, prune by retrieval density."""

    GRAPH = "graph"
    """Edges with provenance. Multi-hop traversal, prune by edge weight."""

    DOCUMENT = "document"
    """Human-editable markdown in the vault. Under user control, never
    auto-deleted — RFC §9 calls this the strategic human interface."""


class RetrievalProtocol(str, enum.Enum):
    """How a domain is queried. RFC §4.1's rightmost column."""

    EXACT_KEY = "exact_key"
    RELATIONAL_SCAN = "relational_scan"
    FULL_TEXT = "full_text"
    VECTOR_SIMILARITY = "vector_similarity"
    GRAPH_TRAVERSAL = "graph_traversal"
    DOCUMENT_PREFETCH = "document_prefetch"
    TIME_RANGE = "time_range"


class MemoryDomain(str, enum.Enum):
    """The RFC §4.1 domain names, retained as vocabulary."""

    WORKING = "working"
    LONG_TERM_DISTILLED = "long_term_distilled"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    TASK = "task"
    SESSION = "session"
    TEMPORAL = "temporal"
    NARRATIVE = "narrative"
    REFLECTION = "reflection"
    STRATEGIC = "strategic"
    SKILL = "skill"
    TOOL = "tool"
    RELATIONSHIP = "relationship"
    IDENTITY = "identity"
    OPERATIONAL = "operational"
    ENVIRONMENTAL = "environmental"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class DomainPolicy:
    """Lifecycle rules for one domain."""

    domain: MemoryDomain
    mechanism: MemoryMechanism
    retrieval: RetrievalProtocol
    decay_lambda: float
    """λ in ``C(t) = C₀·e^(-λt)``, t in days. 0.0 means never decays."""

    prune_floor: float
    """Effective confidence below which the record is evicted."""

    immutable: bool = False
    """Immutable domains are never pruned or rewritten by the curator."""

    narrative: bool = False
    """Narrative domains carry prose and long-horizon framing. They are
    valuable to the *planner* and actively harmful to a *worker*, which needs
    file paths and primitives — RFC §2.2's context-separation rationale. The
    worker context builder blocks these outright."""

    def confidence_at(self, initial: float, idle_days: float) -> float:
        """Effective confidence after ``idle_days`` without a query."""
        import math

        if self.decay_lambda <= 0.0:
            return initial
        return initial * math.exp(-self.decay_lambda * max(0.0, idle_days))

    def is_stale(self, initial: float, idle_days: float) -> bool:
        """Whether this record should be evicted from hot serving."""
        if self.immutable:
            return False
        return self.confidence_at(initial, idle_days) < self.prune_floor


#: The RFC §4.1 matrix, made executable. Decay coefficients are taken directly
#: from the RFC's "Decay Rate (λ)" column.
DOMAINS: Final[dict[MemoryDomain, DomainPolicy]] = {
    MemoryDomain.WORKING: DomainPolicy(
        MemoryDomain.WORKING, MemoryMechanism.VOLATILE, RetrievalProtocol.EXACT_KEY, 0.0, 0.0
    ),
    MemoryDomain.LONG_TERM_DISTILLED: DomainPolicy(
        MemoryDomain.LONG_TERM_DISTILLED,
        MemoryMechanism.RELATIONAL,
        RetrievalProtocol.RELATIONAL_SCAN,
        0.001,
        0.15,
    ),
    MemoryDomain.EPISODIC: DomainPolicy(
        MemoryDomain.EPISODIC,
        MemoryMechanism.IMMUTABLE_LOG,
        RetrievalProtocol.FULL_TEXT,
        0.0,
        0.0,
        immutable=True,
    ),
    MemoryDomain.SEMANTIC: DomainPolicy(
        MemoryDomain.SEMANTIC,
        MemoryMechanism.VECTOR,
        RetrievalProtocol.VECTOR_SIMILARITY,
        0.002,
        0.30,
    ),
    MemoryDomain.PROCEDURAL: DomainPolicy(
        MemoryDomain.PROCEDURAL,
        MemoryMechanism.RELATIONAL,
        RetrievalProtocol.EXACT_KEY,
        0.0,
        0.40,
    ),
    MemoryDomain.TASK: DomainPolicy(
        MemoryDomain.TASK, MemoryMechanism.RELATIONAL, RetrievalProtocol.EXACT_KEY, 0.0, 0.0
    ),
    MemoryDomain.SESSION: DomainPolicy(
        MemoryDomain.SESSION, MemoryMechanism.VOLATILE, RetrievalProtocol.EXACT_KEY, 0.0, 0.0
    ),
    MemoryDomain.TEMPORAL: DomainPolicy(
        MemoryDomain.TEMPORAL,
        MemoryMechanism.RELATIONAL,
        RetrievalProtocol.TIME_RANGE,
        0.01,
        0.15,
    ),
    MemoryDomain.NARRATIVE: DomainPolicy(
        MemoryDomain.NARRATIVE,
        MemoryMechanism.DOCUMENT,
        RetrievalProtocol.DOCUMENT_PREFETCH,
        0.0,
        0.0,
        narrative=True,
    ),
    MemoryDomain.REFLECTION: DomainPolicy(
        MemoryDomain.REFLECTION,
        MemoryMechanism.VECTOR,
        RetrievalProtocol.VECTOR_SIMILARITY,
        0.002,
        0.20,
        narrative=True,
    ),
    MemoryDomain.STRATEGIC: DomainPolicy(
        MemoryDomain.STRATEGIC,
        MemoryMechanism.DOCUMENT,
        RetrievalProtocol.DOCUMENT_PREFETCH,
        0.0,
        0.0,
        immutable=True,
        narrative=True,
    ),
    MemoryDomain.SKILL: DomainPolicy(
        MemoryDomain.SKILL, MemoryMechanism.RELATIONAL, RetrievalProtocol.EXACT_KEY, 0.0, 0.40
    ),
    MemoryDomain.TOOL: DomainPolicy(
        MemoryDomain.TOOL,
        MemoryMechanism.RELATIONAL,
        RetrievalProtocol.RELATIONAL_SCAN,
        0.05,
        0.15,
    ),
    MemoryDomain.RELATIONSHIP: DomainPolicy(
        MemoryDomain.RELATIONSHIP,
        MemoryMechanism.GRAPH,
        RetrievalProtocol.GRAPH_TRAVERSAL,
        0.004,
        0.10,
    ),
    MemoryDomain.IDENTITY: DomainPolicy(
        MemoryDomain.IDENTITY,
        MemoryMechanism.DOCUMENT,
        RetrievalProtocol.DOCUMENT_PREFETCH,
        0.0,
        0.0,
        immutable=True,
        narrative=True,
    ),
    MemoryDomain.OPERATIONAL: DomainPolicy(
        MemoryDomain.OPERATIONAL, MemoryMechanism.VOLATILE, RetrievalProtocol.EXACT_KEY, 1.0, 0.0
    ),
    MemoryDomain.ENVIRONMENTAL: DomainPolicy(
        MemoryDomain.ENVIRONMENTAL,
        MemoryMechanism.RELATIONAL,
        RetrievalProtocol.EXACT_KEY,
        0.0,
        0.0,
    ),
    MemoryDomain.WORKSPACE: DomainPolicy(
        MemoryDomain.WORKSPACE, MemoryMechanism.RELATIONAL, RetrievalProtocol.EXACT_KEY, 0.0, 0.0
    ),
}


def domain_policy(domain: MemoryDomain | str) -> DomainPolicy:
    """Look up a policy, accepting either the enum or its string value."""
    if isinstance(domain, str):
        try:
            domain = MemoryDomain(domain)
        except ValueError as exc:
            raise KeyError(f"unknown memory domain {domain!r}") from exc
    return DOMAINS[domain]


def narrative_domains() -> frozenset[str]:
    """Domain values a worker context packet must never contain.

    Used by the worker context builder to enforce RFC §2.2's separation: the
    worker gets file paths and primitives, never the long-horizon narrative
    that would inflate its packet and leak strategy into an execution cell.
    """
    return frozenset(d.value for d, p in DOMAINS.items() if p.narrative)


# ---------------------------------------------------------------------------
# Tier ladder (PLAN:integration-fix:P009)
# ---------------------------------------------------------------------------
#
# ADR-0037 (docs/adr/0037-tier-layer-and-replay.md) decides the T0-T4 ladder as
# a *layer over* the domain matrix above, not a replacement for it: retention
# (tier TTL) decides *survival*, `DomainPolicy.prune_floor` decides *hot-set
# membership*, and neither may shorten the other's guarantee. This module owns
# the pure policy data; the W3 sweeper is the sole writer of tier moves and the
# sole enforcer of that composition — nothing here reads settings, the clock,
# or the hot table, and `DomainPolicy` gains no tier field (the sweeper SELECTs
# the tier column itself).


@dataclass(frozen=True, slots=True)
class TierPolicy:
    """Retention ladder T0-T4 (ADR-0037).

    ``ttl_hours`` maps each tier to its retention TTL in hours; ``T4`` is
    ``float("inf")`` — the permanent tier, never TTL-expired (ADR-0037 writes
    it as "infinity"; ``float("inf")`` is its faithful float representation).
    The dict is copied per instance via ``default_factory`` so no caller can
    mutate the canonical table through a shared reference.
    """

    ttl_hours: dict[str, float] = field(
        default_factory=lambda: {
            "T0": 4.0,
            "T1": 24.0,
            "T2": 2160.0,
            "T3": 17520.0,
            "T4": float("inf"),
        }
    )

    #: The backfill/default tier. The v5 migration backfills existing rows to
    #: T2 (ADR-0037); any tier value that is unknown, absent, or unrecognised
    #: (a NULL from an old row, a casing mismatch, a future tier this build
    #: does not know) resolves here. Resolution never raises.
    #:
    #: DECISION: `ClassVar`, not a dataclass field — the default tier is
    #: policy, fixed by ADR-0037, not per-instance configuration. A field
    #: would let a caller construct `TierPolicy(DEFAULT_TIER="T0")` and
    #: silently move the default.
    DEFAULT_TIER: ClassVar[str] = "T2"

    def resolve(self, tier: str | None) -> str:
        """Resolve a stored tier to a known tier, defaulting to ``T2``.

        Unknown, absent (``None``), or unrecognised values fall back to the
        default tier rather than raising — a fact written before the tier
        layer existed must remain readable, not become unclassifiable.
        """
        if isinstance(tier, str) and tier in self.ttl_hours:
            return tier
        return self.DEFAULT_TIER

    def ttl_for(self, tier: str | None) -> float:
        """Retention TTL in hours for a tier, defaulting unknown/absent to T2's.

        The final ``2160.0`` is unreachable with the canonical table — it
        keeps the answer a float (never a raise) even for a hand-built
        instance whose ``ttl_hours`` omits the default tier.
        """
        return self.ttl_hours.get(self.resolve(tier), 2160.0)
