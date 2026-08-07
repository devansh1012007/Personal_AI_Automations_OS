"""Confidence decay, staleness, and the eviction sweep."""

from __future__ import annotations

import math
import uuid
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.memory.decay import (
    DecaySweeper,
    effective_confidence,
    half_life_days,
    idle_days,
    importance_index,
    is_stale,
)
from paa.memory.domains import DOMAINS, MemoryDomain, domain_policy, narrative_domains
from paa.storage.relational.database import Database, to_iso, utc_now


class TestIdleDays:
    def test_zero_for_just_queried(self) -> None:
        now = utc_now()
        assert idle_days(now, now=now) == 0.0

    def test_counts_elapsed_days(self) -> None:
        now = utc_now()
        assert idle_days(now - timedelta(days=30), now=now) == pytest.approx(30.0, abs=1e-6)

    def test_future_timestamp_clamps_to_zero(self) -> None:
        """Clock skew must not produce negative idle time and *boost* confidence."""
        now = utc_now()
        assert idle_days(now + timedelta(days=5), now=now) == 0.0

    def test_naive_timestamp_rejected(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="timezone-aware"):
            idle_days(datetime(2026, 1, 1))


class TestEffectiveConfidence:
    def test_no_decay_at_zero_idle(self) -> None:
        now = utc_now()
        assert effective_confidence(0.9, now, MemoryDomain.SEMANTIC, now=now) == pytest.approx(0.9)

    def test_matches_the_closed_form(self) -> None:
        now = utc_now()
        lam = DOMAINS[MemoryDomain.SEMANTIC].decay_lambda
        got = effective_confidence(1.0, now - timedelta(days=100), MemoryDomain.SEMANTIC, now=now)
        assert got == pytest.approx(math.exp(-lam * 100), rel=1e-9)

    def test_zero_lambda_never_decays(self) -> None:
        now = utc_now()
        # Procedural memory is protected structural tooling (λ = 0).
        got = effective_confidence(
            0.8, now - timedelta(days=10_000), MemoryDomain.PROCEDURAL, now=now
        )
        assert got == pytest.approx(0.8)

    def test_faster_lambda_decays_faster(self) -> None:
        now = utc_now()
        old = now - timedelta(days=60)
        tool = effective_confidence(1.0, old, MemoryDomain.TOOL, now=now)        # λ=0.05
        stable = effective_confidence(1.0, old, MemoryDomain.LONG_TERM_DISTILLED, now=now)
        assert tool < stable

    def test_out_of_range_confidence_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0,1\]"):
            effective_confidence(1.5, utc_now(), MemoryDomain.SEMANTIC)

    def test_unknown_domain_rejected(self) -> None:
        with pytest.raises(KeyError, match="unknown memory domain"):
            effective_confidence(0.5, utc_now(), "not_a_domain")

    @given(
        c0=st.floats(min_value=0.0, max_value=1.0),
        days=st.floats(min_value=0.0, max_value=5000.0),
        domain=st.sampled_from(list(MemoryDomain)),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_confidence_stays_in_range_and_never_grows(
        self, c0: float, days: float, domain: MemoryDomain
    ) -> None:
        now = utc_now()
        got = effective_confidence(c0, now - timedelta(days=days), domain, now=now)
        assert 0.0 <= got <= 1.0
        assert got <= c0 + 1e-12  # decay is monotone non-increasing


class TestStaleness:
    def test_fresh_fact_is_not_stale(self) -> None:
        now = utc_now()
        assert not is_stale(1.0, now, MemoryDomain.SEMANTIC, now=now)

    def test_very_old_fact_is_stale(self) -> None:
        now = utc_now()
        # semantic: λ=0.002, floor=0.30 -> ln(1/0.3)/0.002 ≈ 602 days
        assert is_stale(1.0, now - timedelta(days=900), MemoryDomain.SEMANTIC, now=now)

    def test_immutable_domain_is_never_stale(self) -> None:
        """Episodic history and identity are permanent by design."""
        now = utc_now()
        ancient = now - timedelta(days=100_000)
        assert not is_stale(0.0, ancient, MemoryDomain.EPISODIC, now=now)
        assert not is_stale(0.0, ancient, MemoryDomain.IDENTITY, now=now)
        assert not is_stale(0.0, ancient, MemoryDomain.STRATEGIC, now=now)


class TestImportanceIndex:
    def test_use_count_reinforces(self) -> None:
        now = utc_now()
        old = now - timedelta(days=300)
        cold = importance_index(0, 0.5, old, MemoryDomain.SEMANTIC, now=now)
        hot = importance_index(50, 0.5, old, MemoryDomain.SEMANTIC, now=now)
        assert hot > cold

    def test_clamped_to_one(self) -> None:
        """The RFC leaves this unbounded; an importance of 40 would swamp ranking."""
        got = importance_index(100_000, 1.0, utc_now(), MemoryDomain.SEMANTIC)
        assert got == 1.0

    def test_negative_use_count_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            importance_index(-1, 0.5, utc_now(), MemoryDomain.SEMANTIC)

    @given(
        use_count=st.integers(min_value=0, max_value=10_000),
        importance=st.floats(min_value=0.0, max_value=1.0),
        days=st.floats(min_value=0.0, max_value=3000.0),
    )
    @hyp_settings(max_examples=150, deadline=None)
    def test_always_in_unit_range(
        self, use_count: int, importance: float, days: float
    ) -> None:
        now = utc_now()
        got = importance_index(
            use_count, importance, now - timedelta(days=days), MemoryDomain.SEMANTIC, now=now
        )
        assert 0.0 <= got <= 1.0


class TestHalfLife:
    @pytest.mark.parametrize(
        ("lam", "expected"),
        [(0.001, 693.147), (0.01, 69.3147), (0.05, 13.8629), (0.002, 346.574)],
    )
    def test_known_values(self, lam: float, expected: float) -> None:
        assert half_life_days(lam) == pytest.approx(expected, rel=1e-4)

    def test_zero_lambda_is_infinite(self) -> None:
        assert half_life_days(0.0) == math.inf


class TestDomainPolicies:
    def test_every_domain_has_a_policy(self) -> None:
        assert set(DOMAINS) == set(MemoryDomain)

    def test_policies_are_internally_consistent(self) -> None:
        for domain, policy in DOMAINS.items():
            assert policy.domain is domain
            assert policy.decay_lambda >= 0.0
            assert 0.0 <= policy.prune_floor <= 1.0
            if policy.immutable:
                assert policy.decay_lambda == 0.0, f"{domain} is immutable but decays"

    def test_narrative_domains_are_the_prose_ones(self) -> None:
        """The worker context builder blocks exactly this set (RFC §2.2)."""
        assert narrative_domains() == {
            MemoryDomain.NARRATIVE.value,
            MemoryDomain.REFLECTION.value,
            MemoryDomain.STRATEGIC.value,
            MemoryDomain.IDENTITY.value,
        }

    def test_lookup_accepts_string_or_enum(self) -> None:
        assert domain_policy("semantic") is domain_policy(MemoryDomain.SEMANTIC)


class TestDecaySweeper:
    async def _insert_fact(
        self, db: Database, *, domain: str, confidence: float, idle: float
    ) -> str:
        entity_id = str(uuid.uuid4())
        fact_id = str(uuid.uuid4())
        now = utc_now()
        await db.execute(
            "INSERT INTO hot_serving_entity_index "
            "(id, class, canonical_name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (entity_id, "project", f"entity-{entity_id[:8]}", to_iso(now), to_iso(now)),
        )
        await db.execute(
            "INSERT INTO hot_serving_active_facts "
            "(id, entity_id, predicate, object_value, memory_domain, initial_confidence,"
            " created_at, last_queried_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                fact_id,
                entity_id,
                "status",
                "active",
                domain,
                confidence,
                to_iso(now),
                to_iso(now - timedelta(days=idle)),
            ),
        )
        return fact_id

    async def test_evicts_only_decayed_facts(self, db: Database) -> None:
        fresh = await self._insert_fact(db, domain="semantic", confidence=1.0, idle=1)
        stale = await self._insert_fact(db, domain="semantic", confidence=1.0, idle=2000)

        report = await DecaySweeper(db).sweep()

        assert report.scanned == 2
        assert report.evicted == 1
        remaining = {
            r["id"] for r in await db.fetch_all("SELECT id FROM hot_serving_active_facts")
        }
        assert remaining == {fresh}
        assert stale not in remaining

    async def test_immutable_domains_are_skipped(self, db: Database) -> None:
        await self._insert_fact(db, domain="episodic", confidence=0.0, idle=100_000)
        report = await DecaySweeper(db).sweep()

        assert report.skipped_immutable == 1
        assert report.evicted == 0
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1

    async def test_dry_run_deletes_nothing(self, db: Database) -> None:
        await self._insert_fact(db, domain="temporal", confidence=1.0, idle=1000)
        report = await DecaySweeper(db).sweep(dry_run=True)

        assert report.evicted == 1
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1

    async def test_sweep_is_idempotent(self, db: Database) -> None:
        """The sweep runs every 6 hours; re-running must be a no-op."""
        await self._insert_fact(db, domain="semantic", confidence=1.0, idle=2000)
        await self._insert_fact(db, domain="semantic", confidence=1.0, idle=1)

        first = await DecaySweeper(db).sweep()
        second = await DecaySweeper(db).sweep()

        assert first.evicted == 1
        assert second.evicted == 0
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1

    async def test_unknown_domain_is_reported_not_deleted(self, db: Database) -> None:
        """A typo in a domain name must never silently destroy memory."""
        await self._insert_fact(db, domain="typo_domain", confidence=0.0, idle=99_999)
        report = await DecaySweeper(db).sweep()

        assert report.errors == 1
        assert report.evicted == 0
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1

    async def test_batching_covers_everything(self, db: Database) -> None:
        for _ in range(12):
            await self._insert_fact(db, domain="temporal", confidence=1.0, idle=3000)

        report = await DecaySweeper(db, batch_size=5).sweep()

        assert report.scanned == 12
        assert report.evicted == 12
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_touch_resets_idle_and_counts_use(self, db: Database) -> None:
        fact_id = await self._insert_fact(db, domain="semantic", confidence=1.0, idle=2000)
        sweeper = DecaySweeper(db)

        await sweeper.touch(fact_id)
        report = await sweeper.sweep()

        assert report.evicted == 0, "a just-read fact must not be evicted"
        assert await db.fetch_value(
            "SELECT use_count FROM hot_serving_active_facts WHERE id = ?", (fact_id,)
        ) == 1

    async def test_touch_many_batches(self, db: Database) -> None:
        ids = [
            await self._insert_fact(db, domain="semantic", confidence=1.0, idle=2000)
            for _ in range(5)
        ]
        await DecaySweeper(db).touch_many(ids)

        counts = await db.fetch_all("SELECT use_count FROM hot_serving_active_facts")
        assert [c["use_count"] for c in counts] == [1] * 5

    async def test_archives_before_eviction(self, db: Database) -> None:
        """Evicted facts are compressed to cold storage, not destroyed."""

        class Recorder:
            def __init__(self) -> None:
                self.records: list[dict] = []

            async def record(self, *, channel, raw_payload, external_id):
                self.records.append(
                    {"channel": channel, "payload": raw_payload, "external_id": external_id}
                )

        recorder = Recorder()
        await self._insert_fact(db, domain="temporal", confidence=1.0, idle=3000)

        report = await DecaySweeper(db, archive_writer=recorder).sweep()

        assert report.archived == 1
        assert recorder.records[0]["channel"] == "memory_eviction"
        assert "final_confidence" in recorder.records[0]["payload"]

    async def test_archive_failure_does_not_block_eviction(self, db: Database) -> None:
        class Broken:
            async def record(self, **_):
                raise RuntimeError("cold lake unavailable")

        await self._insert_fact(db, domain="temporal", confidence=1.0, idle=3000)
        report = await DecaySweeper(db, archive_writer=Broken()).sweep()

        assert report.evicted == 1
        assert report.archived == 0
