"""Confidence decay and active forgetting.

Implements RFC §4.2. The governing idea is that stored confidence is *never*
mutated by the decay sweep — it is always recomputed from an immutable
``C₀`` and an elapsed idle time:

.. math:: C(t) = C_0 \\cdot e^{-\\lambda t}

Deriving rather than storing matters for crash safety. If the sweep wrote
decayed values back, a crash halfway through a pass would leave some records
decayed and some not, with no way to tell which — and re-running the sweep
would decay the already-decayed ones a second time. Deriving makes the sweep
idempotent and interruptible by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from paa.memory.domains import MemoryDomain, domain_policy

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = [
    "DecayReport",
    "DecaySweeper",
    "effective_confidence",
    "idle_days",
    "importance_index",
    "is_stale",
]

log = structlog.get_logger(__name__)


def idle_days(last_queried_at: datetime, *, now: datetime | None = None) -> float:
    """Days since a record was last read by the context builder.

    RFC §4.2 defines ``t`` as idle time, not age: a fact queried daily stays
    fresh forever, which is the behaviour that makes decay a *usefulness*
    signal rather than a clock.
    """
    from paa.storage.relational.database import utc_now

    reference = now or utc_now()
    if last_queried_at.tzinfo is None:
        raise ValueError("last_queried_at must be timezone-aware")
    return max(0.0, (reference - last_queried_at).total_seconds() / 86_400.0)


def effective_confidence(
    initial_confidence: float,
    last_queried_at: datetime,
    domain: MemoryDomain | str,
    *,
    now: datetime | None = None,
) -> float:
    """Current confidence of a fact. Never stored — always derived."""
    if not 0.0 <= initial_confidence <= 1.0:
        raise ValueError(f"initial_confidence must be in [0,1], got {initial_confidence}")
    policy = domain_policy(domain)
    return policy.confidence_at(initial_confidence, idle_days(last_queried_at, now=now))


def is_stale(
    initial_confidence: float,
    last_queried_at: datetime,
    domain: MemoryDomain | str,
    *,
    floor: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether a fact has decayed below its eviction threshold."""
    policy = domain_policy(domain)
    if policy.immutable:
        return False
    threshold = policy.prune_floor if floor is None else floor
    return effective_confidence(initial_confidence, last_queried_at, domain, now=now) < threshold


def importance_index(
    use_count: int,
    initial_importance: float,
    last_queried_at: datetime,
    domain: MemoryDomain | str,
    *,
    reinforcement: float = 0.01,
    now: datetime | None = None,
) -> float:
    """Memory importance, RFC §15.12.

    .. math:: I = (\\text{UseCount} \\cdot \\delta) + I_0 \\cdot e^{-\\lambda t}

    The use-count term is reinforcement: a fact the context builder keeps
    reaching for earns durability regardless of how old it is. The result is
    clamped to ``[0, 1]`` — the RFC leaves it unbounded, which would let a
    frequently-used fact accumulate an importance of 40 and dominate every
    ranking it appears in.
    """
    if use_count < 0:
        raise ValueError(f"use_count must be non-negative, got {use_count}")
    policy = domain_policy(domain)
    decayed = policy.confidence_at(initial_importance, idle_days(last_queried_at, now=now))
    return min(1.0, max(0.0, use_count * reinforcement + decayed))


@dataclass(slots=True)
class DecayReport:
    """Outcome of one sweep."""

    scanned: int = 0
    evicted: int = 0
    archived: int = 0
    skipped_immutable: int = 0
    errors: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "evicted": self.evicted,
            "archived": self.archived,
            "skipped_immutable": self.skipped_immutable,
            "errors": self.errors,
        }


class DecaySweeper:
    """Evicts decayed facts from hot serving on a schedule.

    RFC §4.2 runs this every 6 hours. Evicted facts are compressed to a
    one-line summary in the cold lake before deletion, so nothing is truly
    lost — hot serving is an index of what is *currently useful*, not an
    archive.
    """

    def __init__(
        self,
        db: Database,
        *,
        archive_writer: object | None = None,
        batch_size: int = 500,
    ) -> None:
        self._db = db
        self._archive = archive_writer
        self._batch = batch_size

    async def sweep(self, *, now: datetime | None = None, dry_run: bool = False) -> DecayReport:
        """Scan live facts and evict those below their domain's floor.

        Processes in batches so a large hot set cannot pin the whole table in
        memory on a RAM-constrained host.

        Pagination is **keyset**, not ``OFFSET``. Offset pagination is
        incorrect whenever the scan deletes as it goes: evicting 5 rows from
        the first page shifts every later row 5 positions earlier, so
        ``OFFSET 5`` on the next page skips 5 rows that were never examined.
        Those records would then never be evicted by any sweep — hot serving
        would grow without bound in a way no error ever reports. Advancing on
        ``id > last_seen`` is stable under concurrent deletion.
        """
        from paa.storage.relational.database import from_iso, utc_now

        reference = now or utc_now()
        report = DecayReport()
        cursor = ""

        while True:
            rows = await self._db.fetch_all(
                "SELECT id, entity_id, predicate, object_value, memory_domain, "
                "       initial_confidence, last_queried_at "
                "FROM hot_serving_active_facts WHERE superseded_by IS NULL AND id > ? "
                "ORDER BY id LIMIT ?",
                (cursor, self._batch),
            )
            if not rows:
                break
            cursor = rows[-1]["id"]

            doomed: list[tuple[str, dict[str, object]]] = []
            for row in rows:
                report.scanned += 1
                try:
                    policy = domain_policy(row["memory_domain"])
                except KeyError:
                    # An unknown domain must not be silently deleted; leave it
                    # and report, so a typo cannot quietly destroy memory.
                    report.errors += 1
                    log.warning("decay.unknown_domain", domain=row["memory_domain"])
                    continue

                if policy.immutable:
                    report.skipped_immutable += 1
                    continue

                confidence = effective_confidence(
                    row["initial_confidence"],
                    from_iso(row["last_queried_at"]),
                    policy.domain,
                    now=reference,
                )
                if confidence < policy.prune_floor:
                    doomed.append(
                        (
                            row["id"],
                            {
                                "entity_id": row["entity_id"],
                                "predicate": row["predicate"],
                                "object_value": row["object_value"],
                                "domain": row["memory_domain"],
                                "final_confidence": round(confidence, 6),
                            },
                        )
                    )

            if doomed and not dry_run:
                report.archived += await self._archive_batch(doomed)
                await self._db.execute_many(
                    "DELETE FROM hot_serving_active_facts WHERE id = ?",
                    [(fact_id,) for fact_id, _ in doomed],
                )
            report.evicted += len(doomed)

        log.info("decay.sweep_completed", dry_run=dry_run, **report.summary())
        return report

    async def _archive_batch(self, doomed: list[tuple[str, dict[str, object]]]) -> int:
        """Compress evicted facts into cold storage. Best-effort.

        An archive failure must not block eviction — but it also must not
        pass silently, so it is logged at error level.
        """
        if self._archive is None:
            return 0
        written = 0
        for fact_id, summary in doomed:
            try:
                await self._archive.record(  # type: ignore[attr-defined]
                    channel="memory_eviction",
                    raw_payload={"fact_id": fact_id, **summary},
                    external_id=f"evict:{fact_id}",
                )
                written += 1
            except Exception as exc:
                log.error("decay.archive_failed", fact_id=fact_id, error=str(exc))
        return written

    async def touch(self, fact_id: str, *, now: datetime | None = None) -> None:
        """Record that the context builder read a fact.

        Resets the idle clock and increments the use count, which is what
        feeds reinforcement in :func:`importance_index`.
        """
        from paa.storage.relational.database import to_iso, utc_now

        await self._db.execute(
            "UPDATE hot_serving_active_facts "
            "SET last_queried_at = ?, use_count = use_count + 1 WHERE id = ?",
            (to_iso(now or utc_now()), fact_id),
        )

    async def touch_many(self, fact_ids: list[str], *, now: datetime | None = None) -> None:
        """Batch variant. One transaction rather than N."""
        from paa.storage.relational.database import to_iso, utc_now

        if not fact_ids:
            return
        stamp = to_iso(now or utc_now())
        await self._db.execute_many(
            "UPDATE hot_serving_active_facts "
            "SET last_queried_at = ?, use_count = use_count + 1 WHERE id = ?",
            [(stamp, fid) for fid in fact_ids],
        )


def half_life_days(decay_lambda: float) -> float:
    """Days until confidence halves. Useful for reasoning about λ choices.

    ``λ = 0.001`` -> ~693 days; ``λ = 0.01`` -> ~69 days; ``λ = 0.05`` -> ~14 days.
    """
    if decay_lambda <= 0:
        return math.inf
    return math.log(2) / decay_lambda
