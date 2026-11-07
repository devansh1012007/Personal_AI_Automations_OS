"""T-009c-P009c: the tier sweeper (PLAN:integration-fix:P009).

T10 (clock-injected move + event-before-row-move order), T10b sweeper half
(rebuild == live column via `apply_tier_event`), dry-run zero-write,
double-emit dedupe by the full ADR-0008 key, crash-between recovery, union
precedence both halves, boundary/empty/unknown/immutable, batching, and the
curator DECAY-tail seam. Tmp `db` + `ledger` fixtures only; no shared DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
import structlog.testing

from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import apply_tier_event, replay
from paa.memory.curator import MemoryCurator
from paa.memory.decay import DecaySweeper
from paa.memory.tier import TIER_SWEEPER_CORRELATION_ID, TierSweeper
from paa.storage.relational.database import Database, to_iso, utc_now

CID = TIER_SWEEPER_CORRELATION_ID

# T0 in the brief's "now=T0+4h1s" is an epoch named T0; every test picks its
# own sweep instant and stamps facts relative to it.
EPOCH = utc_now()


async def _insert_fact(
    db: Database,
    *,
    domain: str = "semantic",
    tier: str = "T0",
    stamp: datetime | None,
    created: datetime | None = None,
    confidence: float = 1.0,
    idle: timedelta = timedelta(days=1),
) -> str:
    """Seed one fact. `stamp=None` writes SQL NULL (a backfilled row)."""
    entity_id = str(uuid.uuid4())
    fact_id = str(uuid.uuid4())
    now = EPOCH
    created_instant = created if created is not None else now
    await db.execute(
        "INSERT INTO hot_serving_entity_index "
        "(id, class, canonical_name, created_at, updated_at) VALUES (?,?,?,?,?)",
        (entity_id, "project", f"entity-{entity_id[:8]}", to_iso(now), to_iso(now)),
    )
    await db.execute(
        "INSERT INTO hot_serving_active_facts "
        "(id, entity_id, predicate, object_value, memory_domain, initial_confidence,"
        " created_at, last_queried_at, tier, tier_updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            fact_id,
            entity_id,
            "status",
            "active",
            domain,
            confidence,
            to_iso(created_instant),
            to_iso(now - idle),
            tier,
            to_iso(stamp) if stamp is not None else None,
        ),
    )
    return fact_id


def _sweeper(db: Database, ledger, *, dry_run: bool = False) -> TierSweeper:
    return TierSweeper(db, ledger, dry_run=dry_run)


class TestT10Move:
    async def test_ttl_expired_moves_one_rung_with_event_first(
        self, db: Database, ledger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fid = await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        due_at = EPOCH + timedelta(seconds=1)  # 5h1s in tier: past the 4h TTL

        # Spy: the row UPDATE must observe the ledger event already landed.
        orig_execute = db.execute
        seen_at_update: list[int] = []

        async def recording_execute(sql, params=()):
            if sql.startswith("UPDATE hot_serving_active_facts"):
                seen_at_update.append(len(await ledger.read_correlation(CID)))
            return await orig_execute(sql, params)

        monkeypatch.setattr(db, "execute", recording_execute)
        report = await _sweeper(db, ledger).sweep(now=due_at)

        assert report.scanned == 1
        assert report.moved == 1
        assert report.demoted == 1
        assert report.archived == 0
        events = await ledger.read_correlation(CID)
        assert len(events) == 1
        (event,) = events
        assert event.event_type is EventType.TIER_DEMOTED
        assert event.payload == {
            "fact_id": fid,
            "from_tier": "T0",
            "to_tier": "T1",
            "reason": "ttl",
        }
        assert event.discriminator == fid + "T1"
        assert event.attempt == 0
        assert event.correlation_id == CID
        # Event-ledger order: the event exists before the row move runs.
        assert seen_at_update == [1]
        row = await db.fetch_one(
            "SELECT tier, tier_updated_at FROM hot_serving_active_facts WHERE id = ?",
            (fid,),
        )
        assert row["tier"] == "T1"
        assert row["tier_updated_at"] == to_iso(due_at)

    async def test_final_rung_emits_archived(self, db: Database, ledger) -> None:
        fid = await _insert_fact(db, tier="T3", stamp=EPOCH)
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(days=365 * 3))
        assert report.moved == 1
        assert report.archived == 1
        assert report.demoted == 0
        (event,) = await ledger.read_correlation(CID)
        assert event.event_type is EventType.TIER_ARCHIVED
        assert event.payload["from_tier"] == "T3"
        assert event.payload["to_tier"] == "T4"
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T4"

    async def test_boundary_exact_ttl_is_not_due(self, db: Database, ledger) -> None:
        fid = await _insert_fact(db, stamp=EPOCH)
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=4))
        assert report.moved == 0
        assert await ledger.count() == 0
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T0"

    async def test_one_second_past_ttl_moves(self, db: Database, ledger) -> None:
        fid = await _insert_fact(db, stamp=EPOCH)
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=4, seconds=1))
        assert report.moved == 1
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T1"

    async def test_empty_hot_table_is_a_noop(self, db: Database, ledger) -> None:
        report = await _sweeper(db, ledger).sweep(now=EPOCH)
        assert report.scanned == 0
        assert report.moved == 0
        assert await ledger.count() == 0

    async def test_unknown_domain_skipped_not_moved(self, db: Database, ledger) -> None:
        fid = await _insert_fact(db, domain="typo_domain", stamp=EPOCH)
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=5))
        assert report.errors == 1
        assert report.moved == 0
        assert await ledger.count() == 0
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T0"

    async def test_immutable_domain_skipped(self, db: Database, ledger) -> None:
        fid = await _insert_fact(
            db, domain="episodic", stamp=EPOCH, idle=timedelta(days=100000)
        )
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=5))
        assert report.skipped_immutable == 1
        assert report.moved == 0
        assert await ledger.count() == 0
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T0"

    async def test_t4_never_due(self, db: Database, ledger) -> None:
        await _insert_fact(db, tier="T4", stamp=EPOCH)
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(days=365 * 10))
        assert report.moved == 0
        assert await ledger.count() == 0

    async def test_batching_covers_everything(self, db: Database, ledger) -> None:
        for _ in range(3):
            await _insert_fact(db, stamp=EPOCH)
        sweeper = TierSweeper(db, ledger, dry_run=False, batch_size=2)
        report = await sweeper.sweep(now=EPOCH + timedelta(hours=5))
        assert report.scanned == 3
        assert report.moved == 3
        tiers = {
            r["tier"] for r in await db.fetch_all("SELECT tier FROM hot_serving_active_facts")
        }
        assert tiers == {"T1"}


class TestNullStampFallback:
    async def test_null_stamp_falls_back_to_created_at(self, db: Database, ledger) -> None:
        old = await _insert_fact(db, tier="T2", stamp=None, created=EPOCH - timedelta(days=100))
        fresh = await _insert_fact(db, tier="T2", stamp=None, created=EPOCH)
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=5))
        assert report.moved == 1  # only the 100-day-old row is past 2160h
        rows = {
            r["id"]: r["tier"]
            for r in await db.fetch_all("SELECT id, tier FROM hot_serving_active_facts")
        }
        assert rows[old] == "T3"
        assert rows[fresh] == "T2"


class TestDryRun:
    async def test_dry_run_writes_zero_events_and_zero_rows(
        self, db: Database, ledger
    ) -> None:
        fid = await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        before = await db.fetch_one(
            "SELECT tier, tier_updated_at FROM hot_serving_active_facts WHERE id = ?",
            (fid,),
        )
        report = await _sweeper(db, ledger, dry_run=True).sweep(
            now=EPOCH + timedelta(hours=5)
        )
        assert report.moved == 1  # the would-be move is counted
        assert await ledger.count() == 0
        after = await db.fetch_one(
            "SELECT tier, tier_updated_at FROM hot_serving_active_facts WHERE id = ?",
            (fid,),
        )
        assert dict(after) == dict(before)
        assert after["tier"] == "T0"

    def test_dry_run_has_no_default(self, db: Database, ledger) -> None:
        """No hardcoded default: the caller must pass the Settings value."""
        with pytest.raises(TypeError):
            TierSweeper(db, ledger)  # type: ignore[call-arg]


class TestT10bRebuild:
    async def test_fold_equals_live_column(self, db: Database, ledger) -> None:
        due_a = await _insert_fact(db, stamp=EPOCH)
        due_b = await _insert_fact(db, tier="T3", stamp=EPOCH)
        not_due = await _insert_fact(db, tier="T4", stamp=EPOCH)
        await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(days=365 * 3))

        events = await ledger.read_correlation(CID)
        assert len(events) == 2
        rebuilt: dict[str, str] = {}
        for event in events:
            rebuilt = apply_tier_event(rebuilt, event)
        live = {
            r["id"]: r["tier"]
            for r in await db.fetch_all("SELECT id, tier FROM hot_serving_active_facts")
        }
        assert {k: live[k] for k in (due_a, due_b)} == rebuilt
        assert rebuilt == {due_a: "T1", due_b: "T4"}
        assert live[not_due] == "T4"  # untouched, not in the fold
        # Replay totality: tier events never perturb task projection.
        projection = replay(events)
        assert projection.unknown_events == []
        ok, problems = await ledger.verify_chain(CID)
        assert ok, problems


class TestIdempotency:
    async def test_double_emit_dedupes_by_full_key(self, db: Database, ledger) -> None:
        fid = await _insert_fact(db, stamp=EPOCH)
        kwargs = {
            "payload": {"fact_id": fid, "from_tier": "T0", "to_tier": "T1", "reason": "ttl"},
            "discriminator": fid + "T1",
            "attempt": 0,
            "recorded_at": EPOCH,
        }
        first = await ledger.append(
            LedgerEvent.create(CID, event_type=EventType.TIER_DEMOTED, **kwargs)
        )
        second = await ledger.append(
            LedgerEvent.create(CID, event_type=EventType.TIER_DEMOTED, **kwargs)
        )
        assert first.event_id == second.event_id
        assert await ledger.count() == 1

    async def test_sweep_twice_same_now_is_idempotent(self, db: Database, ledger) -> None:
        await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        sweeper = _sweeper(db, ledger)
        now = EPOCH + timedelta(hours=1)
        first = await sweeper.sweep(now=now)
        second = await sweeper.sweep(now=now)
        assert first.moved == 1
        assert second.moved == 0
        assert await ledger.count() == 1


class TestCrashBetween:
    async def test_crash_between_event_and_row_move_recovers(
        self, db: Database, ledger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fid = await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        now = EPOCH + timedelta(hours=1)
        orig_execute = db.execute

        async def crashing_execute(sql, params=()):
            if sql.startswith("UPDATE hot_serving_active_facts"):
                raise RuntimeError("simulated crash between event and row move")
            return await orig_execute(sql, params)

        monkeypatch.setattr(db, "execute", crashing_execute)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await _sweeper(db, ledger).sweep(now=now)

        # Event without row move: already-done state, not a loss.
        assert await ledger.count() == 1
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T0"

        # Sweep again: no re-emit, row move completes.
        monkeypatch.undo()
        report = await _sweeper(db, ledger).sweep(now=now)
        assert report.moved == 1
        assert await ledger.count() == 1  # no double-archive, no re-emit
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T1"


class TestUnionPrecedence:
    async def test_tier_move_never_deletes_decay_floor_fact(
        self, db: Database, ledger
    ) -> None:
        """Tier would archive (past 4h TTL); decay floor keeps (fresh, conf 1.0)."""
        fid = await _insert_fact(db, stamp=EPOCH - timedelta(hours=5), idle=timedelta(days=1))
        await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=1))
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row is not None
        assert row["tier"] == "T1"
        # Decay afterwards still keeps it: tier TTL did not shorten the guarantee.
        decay = await DecaySweeper(db).sweep(now=EPOCH + timedelta(hours=1))
        assert decay.evicted == 0
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1

    async def test_below_floor_not_past_ttl_is_untouched_by_tier(
        self, db: Database, ledger
    ) -> None:
        """Below floor but not past TTL: tier sweeper does nothing; decay deletes."""
        fid = await _insert_fact(
            db, tier="T2", stamp=EPOCH, idle=timedelta(days=2000)
        )
        report = await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=1))
        assert report.moved == 0
        assert await ledger.count() == 0
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T2"
        decay = await DecaySweeper(db).sweep(now=EPOCH + timedelta(hours=1))
        assert decay.evicted == 1


class TestCuratorSeam:
    async def test_curator_decay_tail_is_dry_by_default(
        self, db: Database, ledger
    ) -> None:
        """Production seam: settings default (dry) means the curator moves nothing."""
        fid = await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        report = await MemoryCurator(db).run_maintenance(now=EPOCH + timedelta(hours=1))
        assert report.completed_fully
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T0"
        assert await ledger.count() == 0

    async def test_curator_decay_tail_moves_when_sweeper_injected_live(
        self, db: Database, ledger
    ) -> None:
        fid = await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        curator = MemoryCurator(db, tier_sweeper=TierSweeper(db, ledger, dry_run=False))
        report = await curator.run_maintenance(now=EPOCH + timedelta(hours=1))
        assert report.completed_fully
        row = await db.fetch_one(
            "SELECT tier FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert row["tier"] == "T1"
        assert await ledger.count() == 1


class TestObservability:
    async def test_tier_moves_log_lines(self, db: Database, ledger) -> None:
        await _insert_fact(db, stamp=EPOCH - timedelta(hours=5))
        with structlog.testing.capture_logs() as logs:
            await _sweeper(db, ledger).sweep(now=EPOCH + timedelta(hours=1))
        assert any(entry["event"] == "tier.moves" for entry in logs)
