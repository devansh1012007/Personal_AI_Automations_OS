"""Contradiction detection and quarantine.

Implements RFC §4.2. The runtime is hardcoded to *refuse* automatic
resolution: when two facts conflict above threshold, both are degraded and
quarantined, and a human is asked for the tie-break. That is the right call —
a memory system that silently picks a winner between two confident,
contradictory beliefs will confidently act on the wrong one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = [
    "ConflictAssessment",
    "ContradictionDetector",
    "conflict_score",
    "harmonic_confidence",
]

log = structlog.get_logger(__name__)


def harmonic_confidence(c_existing: float, c_new: float) -> float:
    """Harmonic mean of two confidences: ``2·c₁·c₂/(c₁+c₂)``.

    Harmonic rather than arithmetic because it is dominated by the *smaller*
    value: a confident fact conflicting with a shaky one is not a crisis, and
    should not page a human. Only two *mutually* confident facts produce a high
    score.

    Returns 0.0 when both are 0 (the arithmetic is undefined there).
    """
    for name, value in (("c_existing", c_existing), ("c_new", c_new)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0,1], got {value}")
    total = c_existing + c_new
    return 0.0 if total == 0.0 else (2.0 * c_existing * c_new) / total


def conflict_score(
    similarity: float,
    c_existing: float,
    c_new: float,
    *,
    value_divergence: float | None = None,
) -> float:
    """Contradiction metric ``P_conflict``. RFC §4.2.

    .. math::

        P_{conflict} = \\text{Sim}(\\vec{F}_{new}, \\vec{F}_{existing})
                       \\cdot \\frac{2 C_e C_n}{C_e + C_n}

    **A caveat on the RFC's formula.** Candidates are pre-filtered to share an
    identical ``entity_id`` and ``predicate``, so the two fact statements
    already overlap in most of their tokens. Their embedding similarity is
    therefore high and roughly constant — "X's colour is blue" and "X's colour
    is red" score around 0.9 against each other because only one token differs.
    The similarity term does almost no discriminating work; the formula
    effectively reduces to the harmonic mean of the confidences.

    That is *conservative*, so it is safe: it over-flags rather than
    under-flags, and over-flagging costs a human prompt while under-flagging
    costs a wrong belief acted upon. We therefore keep the RFC's formula as the
    default.

    ``value_divergence`` is an optional improvement (SPEC EXTENSION): the
    cosine *distance* between the two **object values alone**, ignoring the
    shared entity/predicate framing. Low value similarity means the values
    genuinely disagree; high value similarity means one refines the other
    ("blue" vs "light blue"), which is not a contradiction. When supplied it
    scales the score, which suppresses false positives on refinements while
    leaving genuine conflicts untouched.
    """
    if not 0.0 <= similarity <= 1.0:
        raise ValueError(f"similarity must be in [0,1], got {similarity}")

    score = similarity * harmonic_confidence(c_existing, c_new)

    if value_divergence is not None:
        if not 0.0 <= value_divergence <= 1.0:
            raise ValueError(f"value_divergence must be in [0,1], got {value_divergence}")
        score *= value_divergence
    return score


@dataclass(slots=True)
class ConflictAssessment:
    """Verdict on one candidate fact pair."""

    is_conflict: bool
    score: float
    incumbent: dict[str, Any]
    challenger: dict[str, Any]
    threshold: float

    @property
    def reason(self) -> str:
        verb = "exceeds" if self.is_conflict else "is below"
        return (
            f"conflict score {self.score:.3f} {verb} threshold {self.threshold:.2f} "
            f"for {self.incumbent.get('entity_id')}/{self.incumbent.get('predicate')}"
        )


class ContradictionDetector:
    """Detects and quarantines mutually contradictory facts."""

    def __init__(
        self,
        db: Database,
        *,
        threshold: float = 0.75,
        degraded_confidence: float = 0.20,
        use_value_divergence: bool = True,
    ) -> None:
        self._db = db
        self._threshold = threshold
        self._degraded = degraded_confidence
        self._use_divergence = use_value_divergence

    async def find_incumbents(self, entity_id: str, predicate: str, value: str) -> list[dict]:
        """Live facts sharing the key but disagreeing on the value.

        RFC §4.2's "inverted predicate filter": same ``entity_id`` and
        ``predicate``, different ``object_value``.
        """
        rows = await self._db.fetch_all(
            "SELECT id, entity_id, predicate, object_value, initial_confidence, "
            "       memory_domain, last_queried_at "
            "FROM hot_serving_active_facts "
            "WHERE entity_id = ? AND predicate = ? AND object_value != ? "
            "  AND superseded_by IS NULL",
            (entity_id, predicate, value),
        )
        return [dict(r) for r in rows]

    def assess(
        self,
        incumbent: dict[str, Any],
        challenger: dict[str, Any],
        similarity: float,
        *,
        value_divergence: float | None = None,
    ) -> ConflictAssessment:
        score = conflict_score(
            similarity,
            float(incumbent.get("confidence", incumbent.get("initial_confidence", 1.0))),
            float(challenger.get("confidence", challenger.get("initial_confidence", 1.0))),
            value_divergence=value_divergence if self._use_divergence else None,
        )
        return ConflictAssessment(
            is_conflict=score >= self._threshold,
            score=score,
            incumbent=incumbent,
            challenger=challenger,
            threshold=self._threshold,
        )

    async def quarantine(
        self,
        assessment: ConflictAssessment,
        *,
        correlation_id: str | None = None,
    ) -> str:
        """Apply the RFC §4.2 conflict protocol.

        1. Block the automated hot-table update (the caller must not upsert).
        2. Degrade both records' confidence to the configured floor.
        3. Move the pair into ``hot_serving_unresolved_buffer``.
        4. The caller parks the task on ``AWAITING_HUMAN_ATTESTATION``.

        Steps 2 and 3 run in one transaction: a crash between them would leave
        facts degraded with no quarantine record explaining why, which looks
        like silent memory corruption.
        """
        from paa.storage.relational.database import to_iso, utc_now

        buffer_id = str(uuid.uuid4())
        incumbent_id = assessment.incumbent.get("id")

        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO hot_serving_unresolved_buffer "
                "(id, entity_id, predicate, incumbent_fact, challenger_fact, conflict_score,"
                " correlation_id, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    buffer_id,
                    str(assessment.incumbent.get("entity_id")),
                    str(assessment.incumbent.get("predicate")),
                    _dumps(assessment.incumbent),
                    _dumps(assessment.challenger),
                    assessment.score,
                    correlation_id,
                    to_iso(utc_now()),
                ),
            )
            if incumbent_id:
                await conn.execute(
                    "UPDATE hot_serving_active_facts SET initial_confidence = ? WHERE id = ?",
                    (self._degraded, incumbent_id),
                )

        log.warning(
            "memory.contradiction_quarantined",
            buffer_id=buffer_id,
            entity_id=assessment.incumbent.get("entity_id"),
            predicate=assessment.incumbent.get("predicate"),
            score=round(assessment.score, 4),
        )
        return buffer_id

    async def open_conflicts(self, *, limit: int = 100) -> list[dict]:
        """Conflicts awaiting a human tie-break, for the dashboard."""
        rows = await self._db.fetch_all(
            "SELECT * FROM hot_serving_unresolved_buffer WHERE resolved_at IS NULL "
            "ORDER BY detected_at ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def resolve(
        self, buffer_id: str, resolution: str, *, restore_confidence: float = 0.9
    ) -> None:
        """Record a human's tie-break.

        ``resolution`` is one of ``incumbent`` / ``challenger`` / ``both`` /
        ``neither``, matching the schema CHECK constraint.
        """
        from paa.storage.relational.database import to_iso, utc_now

        valid = {"incumbent", "challenger", "both", "neither"}
        if resolution not in valid:
            raise ValueError(f"resolution must be one of {sorted(valid)}, got {resolution!r}")

        row = await self._db.fetch_one(
            "SELECT incumbent_fact FROM hot_serving_unresolved_buffer WHERE id = ?",
            (buffer_id,),
        )
        if row is None:
            raise KeyError(f"no unresolved conflict with id {buffer_id}")

        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE hot_serving_unresolved_buffer SET resolved_at = ?, resolution = ? "
                "WHERE id = ?",
                (to_iso(utc_now()), resolution, buffer_id),
            )
            incumbent = _loads(row["incumbent_fact"])
            if resolution in ("incumbent", "both") and incumbent.get("id"):
                await conn.execute(
                    "UPDATE hot_serving_active_facts SET initial_confidence = ? WHERE id = ?",
                    (restore_confidence, incumbent["id"]),
                )
            elif resolution == "neither" and incumbent.get("id"):
                await conn.execute(
                    "DELETE FROM hot_serving_active_facts WHERE id = ?", (incumbent["id"],)
                )

        log.info("memory.contradiction_resolved", buffer_id=buffer_id, resolution=resolution)


def _dumps(value: Any) -> str:
    from paa.storage.relational.database import dumps

    return dumps(value)


def _loads(value: str) -> dict[str, Any]:
    from paa.storage.relational.database import loads

    return loads(value, {}) or {}
