"""Memory subsystem — a filtered, scored interpretation of storage, not storage.

RFC §4 / §12. Layering, bottom to top:

``domains``
    The taxonomy: 18 domain names over 6 real mechanisms (ADR-0014).
``decay``
    Confidence decay and the eviction sweep — derived, never stored.
``contradiction``
    Conflict detection and quarantine; never auto-resolves.
``facts``
    The relational fact/entity repository (the only writer of those tables).
``creator``
    Real-time ETL: cold-lake signal → extracted fact → hot serving.
``curator``
    Nightly maintenance under a wall-clock budget.
``world_model``
    Long-horizon belief state in the markdown vault + episodic compression.
"""

from __future__ import annotations

from paa.memory.contradiction import (
    ConflictAssessment,
    ContradictionDetector,
    conflict_score,
    harmonic_confidence,
)
from paa.memory.creator import (
    BatchReport,
    CandidateFact,
    MalformedSignalError,
    MemoryCreator,
    ProcessingOutcome,
    ProcessingResult,
    RuleBasedExtractor,
    SignalEnvelope,
)
from paa.memory.curator import CurationPhase, CurationReport, MemoryCurator
from paa.memory.decay import (
    DecayReport,
    DecaySweeper,
    effective_confidence,
    idle_days,
    importance_index,
    is_stale,
)
from paa.memory.domains import (
    DOMAINS,
    DomainPolicy,
    MemoryDomain,
    MemoryMechanism,
    RetrievalProtocol,
    domain_policy,
    narrative_domains,
)
from paa.memory.facts import Entity, Fact, FactRepository
from paa.memory.world_model import BeliefDocument, EpisodeSummary, WorldModel

__all__ = [
    "DOMAINS",
    "BatchReport",
    "BeliefDocument",
    "CandidateFact",
    "ConflictAssessment",
    "ContradictionDetector",
    "CurationPhase",
    "CurationReport",
    "DecayReport",
    "DecaySweeper",
    "DomainPolicy",
    "Entity",
    "EpisodeSummary",
    "Fact",
    "FactRepository",
    "MalformedSignalError",
    "MemoryCreator",
    "MemoryCurator",
    "MemoryDomain",
    "MemoryMechanism",
    "ProcessingOutcome",
    "ProcessingResult",
    "RetrievalProtocol",
    "RuleBasedExtractor",
    "SignalEnvelope",
    "WorldModel",
    "conflict_score",
    "domain_policy",
    "effective_confidence",
    "harmonic_confidence",
    "idle_days",
    "importance_index",
    "is_stale",
    "narrative_domains",
]
