"""The T0-T4 tier sweeper (PLAN:integration-fix:P009, ADR-0037).

Where :class:`~paa.memory.decay.DecaySweeper` decides *hot-set membership*
(a fact below its domain ``prune_floor`` leaves hot serving), this sweeper
decides *survival labelling*: a fact whose residency in its current tier
passes that tier's ``TierPolicy.ttl_hours`` advances one rung down the
``T0 -> T1 -> T2 -> T3 -> T4`` ladder. The union precedence (ADR-0037) is
structural here — retention never decides membership, ``prune_floor``
never decides survival, and this module never deletes a fact the decay
guarantee keeps: every move is a ``tier``/``tier_updated_at`` UPDATE.

Two invariants shape the implementation:

**Ledger event before the hot-row move, always.** Each move appends its
``TIER_DEMOTED`` (intermediate rungs ``T1``-``T3``) or ``TIER_ARCHIVED``
(terminal rung ``T4``) event *before* the row UPDATE, through
:class:`~paa.ledger.store.LedgerStore.append`. A crash between the two
leaves an event without a row move, which the next sweep completes: the
row still shows the old tier, so the sweep re-issues the move, the store
dedupes it by the full ADR-0008 key, and the row move lands — no
re-emit, no double-archive, no loss.

**Dry-run writes nothing.** The deleting ticks are gated on
``TierSettings.dry_run`` (declared in ``config.py``, default ``True``),
passed in by the caller — this module reads no settings itself, so it
can never silently inherit a different policy than its caller holds.
With ``dry_run`` the sweep scans and logs the moves it would make and
writes zero events and zero row mutations.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import structlog

from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.memory.domains import TierPolicy, domain_policy
from paa.storage.relational.base import RelationalBackend

__all__ = [
    "TIER_SWEEPER_CORRELATION_ID",
    "TierReport",
    "TierSweeper",
]

log = structlog.get_logger(__name__)


#: All tier events share one deterministic lineage. ADR-0008 idempotency
#: needs the key to be stable across ticks and processes: a random
#: per-tick correlation would mint a distinct key for every retry and
#: defeat double-emit dedupe, while a per-fact id would open one
#: permanently non-terminal head per moved fact. Moves within the lineage
#: are distinguished by the ADR-0008 discriminator (`fact_id + to_tier`).
TIER_SWEEPER_CORRELATION_ID: Final[uuid.UUID] = uuid.uuid5(
    uuid.UUID("9e8c3d62-4c3b-4f57-8d7a-1e2c9a5b6f40"),
    "paa:memory:tier-sweeper",
)

#: The retention ladder, most-durable last. ``T4`` is the permanent tier:
#: :meth:`TierPolicy.ttl_for` returns ``inf`` for it, so it is never due.
_LADDER: Final[tuple[str, ...]] = ("T0", "T1", "T2", "T3", "T4")

#: Payload `reason` for every sweeper move. The vocab tests pin ``"ttl"``
#: as the tier-move reason; keeping one reason keeps the payload shape
#: fixed for every event the sweeper emits.
_REASON_TTL: Final[str] = "ttl"

#: Terminal rung. A move *to* this tier is `TIER_ARCHIVED`; moves to the
#: intermediate rungs are `TIER_DEMOTED`.
_ARCHIVE_TIER: Final[str] = "T4"


@dataclass(slots=True)
class TierReport:
    """Outcome of one tier sweep."""

    scanned: int = 0
    moved: int = 0
    demoted: int = 0
    archived: int = 0
    skipped_immutable: int = 0
    errors: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "moved": self.moved,
            "demoted": self.demoted,
            "archived": self.archived,
            "skipped_immutable": self.skipped_immutable,
            "errors": self.errors,
        }


class TierSweeper:
    """Advances TTL-expired facts one rung down the tier ladder per tick.

    One rung per tick, never a cascade: a tier's TTL is a *residency*
    budget for that rung, so a fact may not be advanced past a rung it
    never occupied. Long-aged facts converge over subsequent ticks, each
    rung with its own ledger event.
    """

    def __init__(
        self,
        db: RelationalBackend,
        store: LedgerStore,
        *,
        dry_run: bool,
        batch_size: int = 500,
        policy: TierPolicy | None = None,
    ) -> None:
        """Create a tier sweeper.

        :param dry_run: the caller's ``TierSettings.dry_run`` value. There
            is deliberately no default to fall back to — a hardcoded
            ``True``/``False`` inside the sweeper would let the production
            default drift from the Settings the operator holds. Callers
            pass the Settings value explicitly (:class:`~paa.memory.curator`
            passes ``get_settings().tier.dry_run`` at construction).
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        self._db = db
        self._store = store
        self._dry_run = dry_run
        self._batch = batch_size
        self._policy = policy if policy is not None else TierPolicy()

    @property
    def dry_run(self) -> bool:
        """The constructor-injected dry-run flag (``TierSettings.dry_run``)."""
        return self._dry_run

    async def sweep(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool | None = None,
    ) -> TierReport:
        """Scan live facts and advance those past their tier TTL by one rung.

        ``dry_run=None`` (the default) uses the constructor-injected flag;
        an explicit value previews or forces a single tick (the curator
        composes its preview flag with the Settings value this way).
        Processes in batches so a large hot set cannot pin the whole
        table in memory.

        Pagination is **keyset**, not ``OFFSET`` — see
        :meth:`~paa.memory.decay.DecaySweeper.sweep` for why offset
        pagination is incorrect while the scan writes.
        """
        from paa.storage.relational.database import from_iso, to_iso, utc_now

        reference = now or utc_now()
        effective_dry = self._dry_run if dry_run is None else dry_run
        report = TierReport()
        cursor = ""

        while True:
            rows = await self._db.fetch_all(
                "SELECT id, memory_domain, tier, tier_updated_at, created_at "
                "FROM hot_serving_active_facts WHERE superseded_by IS NULL AND id > ? "
                "ORDER BY id LIMIT ?",
                (cursor, self._batch),
            )
            if not rows:
                break
            cursor = rows[-1]["id"]

            for row in rows:
                report.scanned += 1
                try:
                    domain = domain_policy(row["memory_domain"])
                except KeyError:
                    # Mirror DecaySweeper: an unknown domain must not be
                    # silently moved; leave it and report.
                    report.errors += 1
                    log.warning("tier.unknown_domain", domain=row["memory_domain"])
                    continue

                if domain.immutable:
                    report.skipped_immutable += 1
                    continue

                move = self._due_move(row, reference, from_iso)
                if move is None:
                    continue

                fact_id = row["id"]
                from_tier, to_tier, event_type = move
                if event_type is EventType.TIER_ARCHIVED:
                    report.archived += 1
                else:
                    report.demoted += 1
                report.moved += 1
                log.info(
                    "tier.moves",
                    fact_id=fact_id,
                    from_tier=from_tier,
                    to_tier=to_tier,
                    event_type=event_type.value,
                    dry_run=effective_dry,
                )
                if effective_dry:
                    continue

                await self._store.append(
                    LedgerEvent.create(
                        TIER_SWEEPER_CORRELATION_ID,
                        event_type,
                        payload={
                            "fact_id": fact_id,
                            "from_tier": from_tier,
                            "to_tier": to_tier,
                            "reason": _REASON_TTL,
                        },
                        # ADR-0008 full key: (correlation_id, event_type,
                        # attempt, discriminator). A redelivery of the same
                        # move carries the same discriminator, so the store
                        # returns the existing event instead of a duplicate.
                        discriminator=fact_id + to_tier,
                        attempt=0,
                        recorded_at=reference,
                    )
                )
                # INVARIANT (ledger-before-side-effect): the UPDATE below
                # runs only after the event above is committed. The
                # `tier = ?` guard makes the write conditional on the row
                # still sitting at the from-tier, so a re-issued move
                # after a crash lands the row move without re-emitting,
                # and a row already moved by a concurrent tick is a no-op
                # rather than a clobber.
                await self._db.execute(
                    "UPDATE hot_serving_active_facts "
                    "SET tier = ?, tier_updated_at = ? WHERE id = ? AND tier = ?",
                    (to_tier, to_iso(reference), fact_id, from_tier),
                )

        log.info("tier.sweep_completed", dry_run=effective_dry, **report.summary())
        return report

    def _due_move(
        self,
        row: dict[str, Any],
        reference: datetime,
        parse_ts: Callable[[Any], datetime],
    ) -> tuple[str, str, EventType] | None:
        """Return (from_tier, to_tier, event_type) if the row is due, else None.

        Due means strictly past `tier_updated_at + ttl_hours` for the
        row's current (resolved) tier. Exact-boundary residency is not
        due: at exactly ``ttl_hours`` the fact has not outlived the tier.
        """
        current = self._policy.resolve(row["tier"])
        ttl_hours = self._policy.ttl_for(current)
        stamp = row["tier_updated_at"]
        # A NULL stamp means "never moved" (backfilled rows): the fact
        # entered its tier when it was created, so creation is the clock
        # base. Without this fallback a backfilled T2 row would never
        # advance at all — nothing else ever stamps it.
        entered = parse_ts(stamp) if stamp is not None else parse_ts(row["created_at"])
        if not (reference - entered).total_seconds() > ttl_hours * 3600.0:
            return None
        index = _LADDER.index(current)
        nxt = _LADDER[index + 1]
        event_type = (
            EventType.TIER_ARCHIVED if nxt == _ARCHIVE_TIER else EventType.TIER_DEMOTED
        )
        return (current, nxt, event_type)
