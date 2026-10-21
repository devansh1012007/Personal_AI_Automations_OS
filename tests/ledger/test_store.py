"""Ledger append semantics: idempotency, ordering, and the hash chain.

What is defended here
---------------------
* A redelivered event is *suppressed* — the incumbent is returned, and
  the caller is not made to catch an exception for the happy path of
  at-least-once delivery.
* A writer that loses a race for a ``state_version`` is *told so*. The
  two look alike from the outside — a rejected insert is a rejected
  insert — and only the re-read tells them apart: the row that took
  the version was filed under a different idempotency key, so the
  re-read by key finds nothing and the append failed for a reason that
  is not a retry.
* The recovery happens on a transaction that is still usable, which is
  what the savepoint is for: PostgreSQL aborts a transaction when a
  statement fails, and the re-read that follows would be refused with
  "current transaction is aborted".
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest

from paa.core.errors import LedgerError
from paa.core.types import ComplexityModality, EventType, new_correlation_id, new_session_id
from paa.ledger.events import GENESIS_HASH, LedgerEvent, compute_idempotency_key
from paa.ledger.store import LedgerStore
from paa.storage.relational.connection import Result
from paa.storage.relational.errors import UniqueViolation


async def _append(store: LedgerStore, cid, etype: EventType, **kw) -> LedgerEvent:
    return await store.append(LedgerEvent.create(cid, etype, **kw))


class TestAppend:
    async def test_assigns_sequence_and_version(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        first = await _append(ledger, cid, EventType.TASK_REQUESTED)
        second = await _append(ledger, cid, EventType.TASK_QUEUED)

        assert first.state_version == 1
        assert second.state_version == 2
        assert second.sequence_id > first.sequence_id

    async def test_versions_are_per_correlation(self, ledger: LedgerStore) -> None:
        """Two lineages each start at version 1 despite sharing the sequence."""
        a, b = new_correlation_id(), new_correlation_id()
        ev_a = await _append(ledger, a, EventType.TASK_REQUESTED)
        ev_b = await _append(ledger, b, EventType.TASK_REQUESTED)

        assert ev_a.state_version == ev_b.state_version == 1
        assert ev_a.sequence_id != ev_b.sequence_id

    async def test_first_event_links_to_genesis(self, ledger: LedgerStore) -> None:
        ev = await _append(ledger, new_correlation_id(), EventType.TASK_REQUESTED)
        assert ev.prev_hash == GENESIS_HASH

    async def test_chain_links_successive_events(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        first = await _append(ledger, cid, EventType.TASK_REQUESTED)
        second = await _append(ledger, cid, EventType.TASK_QUEUED)
        assert second.prev_hash == first.event_hash

    async def test_payload_round_trips(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        payload = {"nested": {"a": [1, 2, 3]}, "unicode": "héllo ✓", "n": None}
        await _append(ledger, cid, EventType.TASK_REQUESTED, payload=payload)
        (stored,) = await ledger.read_correlation(cid)
        assert stored.payload == payload

    async def test_session_and_causation_persist(self, ledger: LedgerStore) -> None:
        cid, sid = new_correlation_id(), new_session_id()
        first = await _append(ledger, cid, EventType.TASK_REQUESTED, session_id=sid)
        second = await store_append_caused_by(ledger, cid, first)

        assert second.session_id == sid
        assert second.causation_id == first.event_id


async def store_append_caused_by(store: LedgerStore, cid, cause: LedgerEvent) -> LedgerEvent:
    return await store.append(
        LedgerEvent(
            correlation_id=cid,
            session_id=cause.session_id,
            causation_id=cause.event_id,
            event_type=EventType.TASK_QUEUED,
        )
    )


class TestIdempotency:
    """The RFC's scheme cannot express a retry; ours can. See ADR-0008."""

    async def test_exact_redelivery_is_suppressed(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        first = await _append(ledger, cid, EventType.EXECUTION_STARTED)
        again = await _append(ledger, cid, EventType.EXECUTION_STARTED)

        assert await ledger.count() == 1
        assert again.sequence_id == first.sequence_id
        assert again.event_hash == first.event_hash

    async def test_redelivery_returns_original_not_error(self, ledger: LedgerStore) -> None:
        """Suppression is the happy path for at-least-once transports."""
        cid = new_correlation_id()
        original = await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"v": 1})
        # Different payload, same logical event -> still suppressed, and the
        # ORIGINAL payload wins. Last-write-wins would corrupt history.
        dup = await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"v": 2})
        assert dup.payload == {"v": 1}
        assert dup.sequence_id == original.sequence_id

    async def test_genuine_retry_is_recorded(self, ledger: LedgerStore) -> None:
        """The bug fix: attempt N+1 is a distinct event, not a constraint error.

        Under the RFC's SHA-256(correlation + type + version) scheme this
        append is impossible, which would deadlock recovery on exactly the
        tasks it exists to rescue.
        """
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.EXECUTION_STARTED, attempt=0)
        await _append(ledger, cid, EventType.EXECUTION_STARTED, attempt=1)
        await _append(ledger, cid, EventType.EXECUTION_STARTED, attempt=2)
        assert await ledger.count() == 3

    async def test_discriminator_separates_concurrent_steps(self, ledger: LedgerStore) -> None:
        """Parallel steps of one plan emit the same event type legitimately."""
        cid = new_correlation_id()
        for step in range(4):
            await _append(
                ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"step-{step}"
            )
        assert await ledger.count() == 4

    def test_key_is_stable_and_sensitive(self) -> None:
        cid = uuid.uuid4()
        base = compute_idempotency_key(cid, EventType.EXECUTION_STARTED)
        assert base == compute_idempotency_key(cid, EventType.EXECUTION_STARTED)
        assert base != compute_idempotency_key(cid, EventType.EXECUTION_STARTED, attempt=1)
        assert base != compute_idempotency_key(cid, EventType.EXECUTION_STARTED, discriminator="s")
        assert base != compute_idempotency_key(cid, EventType.EXECUTION_COMPLETED)
        assert base != compute_idempotency_key(uuid.uuid4(), EventType.EXECUTION_STARTED)

    def test_negative_attempt_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            compute_idempotency_key(uuid.uuid4(), EventType.TASK_QUEUED, attempt=-1)

    async def test_concurrent_identical_appends_write_once(self, ledger: LedgerStore) -> None:
        """Racing coroutines appending the same event must produce one row."""
        cid = new_correlation_id()
        results = await asyncio.gather(
            *(_append(ledger, cid, EventType.TASK_REQUESTED) for _ in range(20))
        )
        assert await ledger.count() == 1
        assert len({r.sequence_id for r in results}) == 1

    async def test_concurrent_distinct_appends_all_land(self, ledger: LedgerStore) -> None:
        """Dense, gap-free versions under concurrency — the atomicity guarantee."""
        cid = new_correlation_id()
        await asyncio.gather(
            *(
                _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")
                for i in range(25)
            )
        )
        events = await ledger.read_correlation(cid)
        assert [e.state_version for e in events] == list(range(1, 26))


class TestChainIntegrity:
    async def test_clean_chain_verifies(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for etype in (
            EventType.TASK_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
        ):
            await _append(ledger, cid, etype)

        ok, problems = await ledger.verify_chain(cid)
        assert ok and problems == []

    async def test_payload_tampering_is_detected(self, ledger: LedgerStore, db) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"goal": "benign"})
        await _append(ledger, cid, EventType.PLAN_COMPILED)

        await db.execute(
            "UPDATE system_state_ledger SET payload = ? WHERE correlation_id = ? "
            "AND state_version = 1",
            ('{"goal":"exfiltrate"}', str(cid)),
        )

        ok, problems = await ledger.verify_chain(cid)
        assert not ok
        assert any("digest mismatch" in p for p in problems)

    async def test_deletion_is_detected_as_version_gap(self, ledger: LedgerStore, db) -> None:
        cid = new_correlation_id()
        for etype in (
            EventType.TASK_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
        ):
            await _append(ledger, cid, etype)

        await db.execute(
            "DELETE FROM system_state_ledger WHERE correlation_id = ? AND state_version = 2",
            (str(cid),),
        )

        ok, problems = await ledger.verify_chain(cid)
        assert not ok
        assert any("version gap" in p or "broken chain" in p for p in problems)

    async def test_verify_all_reports_only_broken_chains(self, ledger: LedgerStore, db) -> None:
        good, bad = new_correlation_id(), new_correlation_id()
        await _append(ledger, good, EventType.TASK_REQUESTED)
        await _append(ledger, bad, EventType.TASK_REQUESTED)
        await db.execute(
            "UPDATE system_state_ledger SET payload='{\"x\":1}' WHERE correlation_id = ?",
            (str(bad),),
        )

        report = await ledger.verify_all_chains()
        assert str(bad) in report
        assert str(good) not in report


class TestAnEventThatWasNeverSealed:
    """A chain check must report an anomaly, not abort on it.

    ``LedgerEvent.state_version`` is optional on the *model* because the
    store assigns it inside the append transaction; a row read back from the
    ledger was sealed, and the column is NOT NULL. The guard exists for the
    case where that does not hold, and the point of it is that one unsealed
    event does not cost the whole lineage its verification.

    The stakes are higher than the lineage: the verification is asked for by
    the boot sweep, which is what recovers the lineages in the first place,
    and an exception raised out of it takes the whole sweep with it.
    """

    async def test_a_missing_version_is_reported_not_raised(self, db) -> None:
        ok, problems = await _UnsealedStore(db).verify_chain(new_correlation_id())

        assert not ok
        assert any("missing state_version" in p for p in problems)


class _UnsealedStore(LedgerStore):
    """A store whose read path returns an event the store never sealed."""

    async def read_correlation(self, correlation_id, *, from_version=0):
        return [
            LedgerEvent(
                correlation_id=correlation_id,
                event_type=EventType.TASK_REQUESTED,
            )
        ]


class TestHeadProjection:
    """The bounded alternative to the RFC's unbounded partial index (ADR-0010)."""

    async def test_open_lineage_is_listed(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.EXECUTION_STARTED)
        heads = await ledger.open_correlations()
        assert [h.correlation_id for h in heads] == [cid]

    async def test_terminal_lineage_drops_out(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.EXECUTION_STARTED)
        await _append(ledger, cid, EventType.MUTATION_COMMITTED)
        assert await ledger.open_correlations() == []

    async def test_head_does_not_grow_with_history(self, ledger: LedgerStore, db) -> None:
        """One row per lineage regardless of event count — the whole point."""
        cid = new_correlation_id()
        for i in range(40):
            await _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")

        rows = await db.fetch_value("SELECT COUNT(*) FROM system_state_correlation_head")
        assert rows == 1
        assert await ledger.count() == 40

    async def test_head_tracks_latest_event(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.PLAN_COMPILED)

        head = await ledger.head(cid)
        assert head is not None
        assert head.latest_event_type is EventType.PLAN_COMPILED
        assert head.latest_state_version == 2
        assert not head.is_terminal


class TestReads:
    async def test_read_correlation_is_ordered(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for i in range(10):
            await _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")

        events = await ledger.read_correlation(cid)
        assert [e.state_version for e in events] == sorted(e.state_version for e in events)

    async def test_read_from_version_is_exclusive(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for i in range(5):
            await _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")

        tail = await ledger.read_correlation(cid, from_version=3)
        assert [e.state_version for e in tail] == [4, 5]

    async def test_events_since_filters_by_type(self, ledger: LedgerStore) -> None:
        from paa.storage.relational.database import utc_now

        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.USER_CORRECTION)

        start = utc_now().replace(year=2000)
        only = await ledger.events_since(start, event_types=[EventType.USER_CORRECTION])
        assert [e.event_type for e in only] == [EventType.USER_CORRECTION]

    async def test_latest_event(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        last = await _append(ledger, cid, EventType.PLAN_COMPILED)
        assert (await ledger.latest_event(cid)).event_id == last.event_id


class TestSnapshots:
    async def test_round_trip(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await ledger.save_snapshot(cid, 7, {"phase": "EXECUTING", "n": 3})
        loaded = await ledger.load_snapshot(cid)
        assert loaded == (7, {"phase": "EXECUTING", "n": 3})

    async def test_latest_snapshot_wins(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await ledger.save_snapshot(cid, 3, {"v": 3})
        await ledger.save_snapshot(cid, 9, {"v": 9})
        version, data = await ledger.load_snapshot(cid)
        assert (version, data) == (9, {"v": 9})

    async def test_missing_snapshot_is_none(self, ledger: LedgerStore) -> None:
        assert await ledger.load_snapshot(new_correlation_id()) is None


class TestModeAndMetadata:
    async def test_execution_mode_persists(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(
                cid, EventType.TASK_REQUESTED, execution_mode=ComplexityModality.MAX
            )
        )
        (stored,) = await ledger.read_correlation(cid)
        assert stored.execution_mode is ComplexityModality.MAX

    async def test_naive_timestamp_rejected(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="timezone-aware"):
            LedgerEvent(
                correlation_id=uuid.uuid4(),
                event_type=EventType.TASK_REQUESTED,
                recorded_at=datetime(2026, 1, 1, 12, 0, 0),
            )


class _RacingConnection:
    """A transaction connection whose ledger insert loses the race.

    The race is not reachable from the outside: the backend's write lock
    and the server's advisory lock both make two writers for one lineage
    serial, so the duplicate branch in ``append`` is a *defensive* path.
    It still has to work — a path that is documented and provably broken
    is worse than no path — and the way to test it is to make the race
    happen.

    The connection reports the pre-check as finding nothing and the
    recovery's re-read as finding the incumbent, and refuses the insert.
    """

    def __init__(self, inner, incumbent) -> None:
        self._inner = inner
        self._incumbent = incumbent
        self._reads = 0
        self.statements: list[str] = []

    def execute(self, sql: str, params=()):
        self.statements.append(sql)
        if "WHERE idempotency_key" in sql:
            self._reads += 1
            if self._reads == 1:
                return Result(_no_rows())
            return Result(_one_row(_row_of(self._incumbent)))
        if "INSERT INTO system_state_ledger" in sql:
            return Result(_refused())
        return self._inner.execute(sql, params)

    async def executemany(self, sql: str, rows) -> None:
        await self._inner.executemany(sql, rows)


class _RacingBackend:
    """A backend whose transactions hand out the racing connection."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.connection: _RacingConnection | None = None

    @property
    def dialect(self):
        return self._inner.dialect

    @asynccontextmanager
    async def transaction(self):
        async with self._inner.transaction() as conn:
            self.connection = _RacingConnection(conn, self._incumbent)
            yield self.connection

    async def fetch_all(self, sql: str, params=()):
        return await self._inner.fetch_all(sql, params)

    async def fetch_one(self, sql: str, params=()):
        return await self._inner.fetch_one(sql, params)

    async def fetch_value(self, sql: str, params=()):
        return await self._inner.fetch_value(sql, params)

    async def execute(self, sql: str, params=()):
        return await self._inner.execute(sql, params)

    async def execute_many(self, sql: str, rows) -> None:
        await self._inner.execute_many(sql, rows)

    async def table_names(self):
        return await self._inner.table_names()

    async def close(self) -> None:
        await self._inner.close()


class _VersionCollisionConnection(_RacingConnection):
    """A connection whose insert loses a race it cannot recover from.

    The same refusal as :class:`_RacingConnection`, but the recovery's
    re-read finds *nothing* — and that difference is the whole point.

    A suppressed duplicate is found by the idempotency key: the row that
    collided carries that key, because it is the same event. Two writers
    that claimed the same ``state_version`` were appending *different*
    events, so the row that took the version is filed under a different
    key — and the re-read by key comes back empty.

    That is the case suppression cannot resolve, and it is the case that
    has to be *reported* rather than returned.
    """

    def __init__(self, inner) -> None:
        super().__init__(inner, incumbent=None)

    def execute(self, sql: str, params=()):
        self.statements.append(sql)
        if "WHERE idempotency_key" in sql:
            # The pre-check finds nothing, and so does the recovery's
            # re-read: the row that took the version is under another key.
            return Result(_no_rows())
        if "INSERT INTO system_state_ledger" in sql:
            return Result(_refused())
        return self._inner.execute(sql, params)


class _VersionCollisionBackend(_RacingBackend):
    """A backend whose transactions hand out the version-collision connection."""

    @asynccontextmanager
    async def transaction(self):
        async with self._inner.transaction() as conn:
            self.connection = _VersionCollisionConnection(conn)
            yield self.connection


async def _no_rows():
    return _FakeCursor([])


async def _one_row(row):
    return _FakeCursor([row])


async def _refused():
    raise UniqueViolation("duplicate idempotency key", substrate="sqlite")


def _row_of(event: LedgerEvent) -> dict:
    """The stored shape of an event, as a row the mapper can read."""
    return {
        "sequence_id": event.sequence_id,
        "event_id": str(event.event_id),
        "correlation_id": str(event.correlation_id),
        "session_id": str(event.session_id) if event.session_id else None,
        "causation_id": str(event.causation_id) if event.causation_id else None,
        "state_version": event.state_version,
        "attempt": event.attempt,
        "discriminator": event.discriminator,
        "event_type": event.event_type.value,
        "execution_mode": event.execution_mode.value,
        "agent_role": event.agent_role,
        "allocated_worker_image": event.allocated_worker_image,
        "payload": event.payload,
        "prev_hash": event.prev_hash,
        "event_hash": event.event_hash,
        "recorded_at": event.recorded_at.isoformat(),
    }


class _FakeCursor:
    """The smallest cursor the contract needs: one row, or none."""

    def __init__(self, rows) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)

    async def fetchmany(self, size):
        return list(self._rows[:size])


class TestTheDuplicateRecoveryPath:
    """The path that handles a writer in another process winning the race."""

    async def test_a_lost_race_returns_the_incumbent(self, db) -> None:
        cid = new_correlation_id()
        store = LedgerStore(db)
        incumbent = await _append(store, cid, EventType.TASK_REQUESTED)

        racing = _RacingBackend(db)
        racing._incumbent = incumbent
        raced_store = LedgerStore(racing)

        result = await raced_store.append(
            LedgerEvent.create(cid, EventType.TASK_REQUESTED)
        )

        assert result.event_id == incumbent.event_id
        assert result.state_version == incumbent.state_version
        assert result.event_hash == incumbent.event_hash

    async def test_a_lost_race_rolls_back_to_the_savepoint(self, db) -> None:
        """The re-read has to happen on a *usable* transaction.

        PostgreSQL aborts a transaction when a statement fails, and the next
        statement is refused with "current transaction is aborted" — so the
        recovery needs the savepoint, or the documented path is the one
        path that cannot work on the server substrate.
        """
        cid = new_correlation_id()
        store = LedgerStore(db)
        incumbent = await _append(store, cid, EventType.TASK_REQUESTED)

        racing = _RacingBackend(db)
        racing._incumbent = incumbent

        await LedgerStore(racing).append(LedgerEvent.create(cid, EventType.TASK_REQUESTED))

        statements = racing.connection.statements if racing.connection else []
        assert any("SAVEPOINT" in s for s in statements)
        assert any("ROLLBACK TO SAVEPOINT" in s for s in statements)

    async def test_the_three_savepoint_statements_name_the_same_savepoint(self, db) -> None:
        """A typo in one of the three would silently disable the recovery.

        The savepoint is written into each of the three statements as a
        literal rather than interpolated — so that no statement in the
        ledger store is assembled at runtime — and a literal can be
        misspelled in a way a constant cannot. A misspelled name would
        make the rollback fail on the server substrate, where it is the
        only thing that makes the re-read possible.
        """
        cid = new_correlation_id()
        store = LedgerStore(db)
        incumbent = await _append(store, cid, EventType.TASK_REQUESTED)

        racing = _RacingBackend(db)
        racing._incumbent = incumbent

        await LedgerStore(racing).append(LedgerEvent.create(cid, EventType.TASK_REQUESTED))

        statements = racing.connection.statements if racing.connection else []
        for expected in (
            "SAVEPOINT ledger_append_insert",
            "ROLLBACK TO SAVEPOINT ledger_append_insert",
            "RELEASE SAVEPOINT ledger_append_insert",
        ):
            assert expected in statements, expected


class TestTheVersionCollision:
    """Two writers, one version — and the failure that has to be reported.

    The duplicate-recovery path answers a rejected insert by re-reading
    the row *by idempotency key*. The key is there → the duplicate was
    ours → the incumbent is returned. The key is absent → the constraint
    that rejected the insert was the one on ``state_version``, another
    writer took the version first, and the append failed for a reason
    that is not a retry.

    Reporting that as a suppressed duplicate would be a lie: the caller
    would be handed a lineage that was never written and told it was
    already there.
    """

    async def test_a_race_for_the_version_is_reported_rather_than_suppressed(
        self, db
    ) -> None:
        """The collision is named, not swallowed.

        The message has to say what happened — two writers raced for the
        same ``state_version`` — because a caller that only sees
        "duplicate" retries, and a retry of a version collision is
        another collision.
        """
        cid = new_correlation_id()
        store = LedgerStore(_VersionCollisionBackend(db))

        with pytest.raises(LedgerError) as excinfo:
            await store.append(LedgerEvent.create(cid, EventType.TASK_REQUESTED))

        message = str(excinfo.value)
        assert "two writers raced for the same state_version" in message
        # The lineage the failure is about, so the operator knows where to
        # look.
        assert excinfo.value.details["correlation_id"] == str(cid)
        assert excinfo.value.details["event_type"] == EventType.TASK_REQUESTED.value

    async def test_the_collision_writes_nothing_to_the_ledger(self, db) -> None:
        """A version collision leaves no trace on the ledger.

        Returning the incumbent — or nothing at all — would tell the
        caller that its event is on disk. It is not, and the caller's
        next decision depends on knowing that.
        """
        cid = new_correlation_id()
        store = LedgerStore(_VersionCollisionBackend(db))

        with pytest.raises(LedgerError):
            await store.append(LedgerEvent.create(cid, EventType.TASK_REQUESTED))

        # Nothing was written, and the lineage has no events.
        assert await LedgerStore(db).count() == 0
        assert await LedgerStore(db).read_correlation(cid) == []

    async def test_the_savepoint_is_released_when_the_collision_is_reported(
        self, db
    ) -> None:
        """The savepoint is released on the way out, error or not.

        A savepoint left open would make the next statement in the same
        transaction a statement inside somebody else's recovery scope.
        The rollback to it is in the same breath: on the server
        substrate it is what makes the re-read possible at all, because a
        failed statement aborts the transaction.
        """
        backend = _VersionCollisionBackend(db)
        store = LedgerStore(backend)

        with pytest.raises(LedgerError):
            await store.append(
                LedgerEvent.create(new_correlation_id(), EventType.TASK_REQUESTED)
            )

        statements = backend.connection.statements if backend.connection else []
        assert "ROLLBACK TO SAVEPOINT ledger_append_insert" in statements
        assert "RELEASE SAVEPOINT ledger_append_insert" in statements

    async def test_the_database_is_usable_after_the_collision_is_reported(
        self, db
    ) -> None:
        """A reported failure must not leave the connection poisoned.

        The append failed inside a transaction, and the transaction is
        rolled back on the way out. The write connection is handed back
        with no transaction open, so the next writer must be able to
        begin one — and the failed append must have written nothing.
        """
        cid = new_correlation_id()
        with pytest.raises(LedgerError):
            await LedgerStore(_VersionCollisionBackend(db)).append(
                LedgerEvent.create(cid, EventType.TASK_REQUESTED)
            )

        store = LedgerStore(db)
        landed = await _append(store, new_correlation_id(), EventType.TASK_REQUESTED)

        assert landed.state_version == 1
        assert await store.count() == 1
        assert await store.read_correlation(cid) == []
