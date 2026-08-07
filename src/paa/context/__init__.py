"""Bounded context construction — RFC §5 and the §15 invariant equations.

The runtime's defence against context-window flooding. Given candidates that
some other layer has already retrieved, this package decides what fits inside a
hard token ceiling, how much of the task's declared information need that
covers, and therefore whether the task may plan, must hydrate more context
first, or has to stop and ask a human.

Layering, bottom to top — nothing imports upwards:

``metrics``
    Pure equations. No dependencies inside the package.
``budget``
    Token accounting and estimation. Depends on config and core only.
``compaction``
    Mechanical text reduction. Depends on ``budget`` for estimation.
``gatherer``
    Packet assembly. Depends on ``metrics`` and ``budget``.

Two RFC equations are corrected here rather than implemented literally; both
are unbounded or undefined as published. See
:func:`~paa.context.metrics.attention_allocation_score` (docs/adr/0012) and
:func:`~paa.context.metrics.hybrid_retrieval_score` (docs/adr/0013).
"""

from __future__ import annotations

from paa.context.budget import (
    CharEstimator,
    TiktokenEstimator,
    TokenBudget,
    TokenEstimator,
    default_estimator,
    estimate_total,
    tiktoken_available,
)
from paa.context.compaction import (
    DEFAULT_ELLIPSIS,
    DEFAULT_MAX_LINE_CHARS,
    ContextCompactor,
)
from paa.context.gatherer import (
    NARRATIVE_MEMORY_DOMAINS,
    BoundedContextGatherer,
    ContextElement,
    ContextPacket,
    RoutingDirective,
)
from paa.context.metrics import (
    ScoredFact,
    attention_allocation_score,
    context_density,
    context_entropy,
    context_pollution_ratio,
    context_utility_score,
    hybrid_retrieval_score,
    memory_importance_index,
    narrative_coherence_score,
    planning_cost,
    provider_reliability,
    skill_rank,
    token_efficiency,
)

__all__ = [
    "DEFAULT_ELLIPSIS",
    "DEFAULT_MAX_LINE_CHARS",
    "NARRATIVE_MEMORY_DOMAINS",
    "BoundedContextGatherer",
    "CharEstimator",
    "ContextCompactor",
    "ContextElement",
    "ContextPacket",
    "RoutingDirective",
    "ScoredFact",
    "TiktokenEstimator",
    "TokenBudget",
    "TokenEstimator",
    "attention_allocation_score",
    "context_density",
    "context_entropy",
    "context_pollution_ratio",
    "context_utility_score",
    "default_estimator",
    "estimate_total",
    "hybrid_retrieval_score",
    "memory_importance_index",
    "narrative_coherence_score",
    "planning_cost",
    "provider_reliability",
    "skill_rank",
    "tiktoken_available",
    "token_efficiency",
]
