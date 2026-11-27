"""Rollback behavior: the walk, the references, the resume (C9.2).

The service behind ``paa rollback``, driven through the real journal, the
real ledger store and the real workspace jail (the conftest discipline).
What is pinned is the contract the CLI maps and the ADR states:

* the walk is **newest-first** and per-effect — one failing compensation
  is visible in the report, does not stop the walk, and a re-run
  completes exactly the remainder;
* every executed compensation rides a **fresh lineage** whose COMPLETED
  rows reference the effect ids they settle (``compensates``), so the
  compensated set is derivable from the ledger alone;
* **no double compensation** — across any number of runs, each effect id
  is settled by exactly one completed compensation row;
* irreversible effects are **reported with their owner call named**, not
  "fixed";
* the exit contract: 0 iff this run compensated at least one effect.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from paa.core.types import EventType
from paa.effects.registry import EffectRegistry
from paa.effects.rollback import (
    STATUS_ALREADY,
    STATUS_COMPENSATED,
    STATUS_FAILED,
    STATUS_IRREVERSIBLE,
    RollbackRefused,
    rollback_scope,
)
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import CallJournal

from .conftest import FakeBlobStore, ScopeBuilder, all_compensates_refs, compensates_refs


class TestTheHappyPath:
    async def test_three_file_effects_all_compensated_newest_first(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        run_id = uuid.uuid4()
        a = await builder.write_file(run_id, 1, "out/a.txt", "alpha")
        b = await builder.write_file(run_id, 2, "out/b.txt", "b-one")
        b2 = await builder.write_file(run_id, 3, "out/b.txt", "b-two")

        report = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )

        assert report.exit_code == 0
        assert [e.status for e in report.entries] == [STATUS_COMPENSATED] * 3
        # Newest-first: the modification of b settles before b's creation
        # (both before a's) — unwinding in reverse application order.
        assert [e.effect_id for e in report.entries] == [
            b2.effect_id,
            b.effect_id,
            a.effect_id,
        ]
        # The world: nothing the scope created or modified survives.
        assert not (workspace / "out/a.txt").exists()
        assert not (workspace / "out/b.txt").exists()

        # The fresh lineage: a rollback run, distinct from the scope, that
        # references every effect id exactly once on its COMPLETED rows.
        lineage = uuid.UUID(report.rollback_lineage)
        assert lineage != run_id
        rows = await ledger.read_correlation(lineage)
        started = [r for r in rows if r.event_type is EventType.PIPELINE_RUN_STARTED]
        assert started[0].payload["run_kind"] == "rollback"
        assert sorted(await compensates_refs(ledger, lineage)) == sorted(
            [a.effect_id, b.effect_id, b2.effect_id]
        )

        # The compensated set is derivable by a reader that holds nothing
        # but the ledger and the blob store — the crash-resume oracle.
        registry = EffectRegistry(ledger, blob_store=blob_store)
        assert await registry.compensated_effect_ids() == {
            a.effect_id,
            b.effect_id,
            b2.effect_id,
        }


class TestPartialFailureAndResume:
    async def test_a_failing_middle_compensation_is_visible_and_the_re_run_completes_it(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        run_id = uuid.uuid4()
        # Pre-run state: b.txt exists; the scope modifies it, so the
        # middle effect's compensation is a *restore*, the one path that
        # fetches bytes by address and can genuinely fail at execution.
        out = workspace / "out"
        out.mkdir()
        (out / "b.txt").write_bytes(b"b-zero")
        a = await builder.write_file(run_id, 1, "out/a.txt", "alpha")
        b = await builder.write_file(run_id, 2, "out/b.txt", "b-one")
        c = await builder.write_file(run_id, 3, "out/c.txt", "gamma")

        assert b.compensation is not None
        prior_ref = b.compensation.prior_bytes_ref
        assert prior_ref is not None
        true_prior = blob_store.get(prior_ref)
        blob_store.corrupt(prior_ref, b"tampered-bytes")

        first = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )

        # Partial is visible: the walk did not stop at the failure, and
        # the failed entry names its effect and its reason.
        assert [e.status for e in first.entries] == [
            STATUS_COMPENSATED,  # c (seq 3, newest)
            STATUS_FAILED,  # b (seq 2, the corrupted restore)
            STATUS_COMPENSATED,  # a (seq 1, oldest)
        ]
        assert first.exit_code == 0  # two compensated this run
        failed = first.entries[1]
        assert failed.effect_id == b.effect_id
        assert "digest" in failed.detail
        # c removed, a removed; b untouched — the restore refused before
        # writing anything, so the file still holds the scope's bytes.
        assert not (workspace / "out/c.txt").exists()
        assert not (workspace / "out/a.txt").exists()
        assert (workspace / "out/b.txt").read_bytes() == b"b-one"

        # The run's own ledger row says partial, not completed.
        rows = await ledger.read_correlation(uuid.UUID(first.rollback_lineage))
        completed = [r for r in rows if r.event_type is EventType.PIPELINE_RUN_COMPLETED]
        assert completed[0].payload["status"] == "partial"

        # The corruption is repaired (the true bytes return under the
        # same address) and the re-run completes exactly the remainder.
        blob_store.corrupt(prior_ref, true_prior)
        second = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )
        assert [e.status for e in second.entries] == [
            STATUS_ALREADY,
            STATUS_COMPENSATED,
            STATUS_ALREADY,
        ]
        assert second.exit_code == 0  # the remainder compensated this run
        # The world has converged on the pre-run state.
        assert (workspace / "out/b.txt").read_bytes() == b"b-zero"

        # Each effect settled exactly once across both runs.
        assert sorted(await all_compensates_refs(ledger)) == sorted(
            [a.effect_id, b.effect_id, c.effect_id]
        )


class TestTheDoubleRollback:
    async def test_a_second_run_is_an_honest_no_op(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        run_id = uuid.uuid4()
        a = await builder.write_file(run_id, 1, "out/a.txt", "alpha")
        b = await builder.write_file(run_id, 2, "out/b.txt", "beta")

        first = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )
        assert first.exit_code == 0

        second = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )
        # Nothing rolled back THIS run: exit 1, every effect already
        # settled, and the report says so rather than claiming work.
        assert second.exit_code == 1
        assert [e.status for e in second.entries] == [STATUS_ALREADY, STATUS_ALREADY]
        assert second.newly_compensated == 0
        assert second.to_json()["summary"] == {
            "effects": 2,
            "compensated": 0,
            "already_compensated": 2,
            "irreversible": 0,
            "failed": 0,
        }
        # No new references: exactly one settled row per effect, ever.
        assert sorted(await all_compensates_refs(ledger)) == sorted(
            [a.effect_id, b.effect_id]
        )
        # The workspace is untouched by the second run.
        assert not (workspace / "out/a.txt").exists()
        assert not (workspace / "out/b.txt").exists()


class TestIrreversibleReporting:
    async def test_irreversible_effects_are_reported_with_their_owner_call(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        run_id = uuid.uuid4()
        await builder.write_file(run_id, 1, "out/a.txt", "alpha")
        mail = await builder.mark_irreversible(run_id, 2)

        report = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )

        # Newest-first: the irreversible marker (seq 2) is walked first,
        # reported, and the walk carries on to the compensable file.
        assert [e.status for e in report.entries] == [
            STATUS_IRREVERSIBLE,
            STATUS_COMPENSATED,
        ]
        entry = report.entries[0]
        assert entry.effect_id == mail.effect_id
        assert entry.kind == "irreversible"
        assert entry.owner_call == f"{run_id}/call:2"
        assert "no compensation exists" in entry.detail
        # The compensable half still happened.
        assert not (workspace / "out/a.txt").exists()
        assert report.exit_code == 0

    async def test_a_scope_of_only_irreversible_effects_rolls_back_nothing(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        run_id = uuid.uuid4()
        await builder.mark_irreversible(run_id, 1)
        await builder.mark_irreversible(run_id, 2)

        report = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )
        assert report.exit_code == 1
        assert report.irreversible == 2
        assert await all_compensates_refs(ledger) == []


class TestRefusalsAndEmptyScopes:
    async def test_compensable_effects_without_a_workspace_and_blob_store_refuse(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        run_id = uuid.uuid4()
        await builder.write_file(run_id, 1, "out/a.txt", "alpha")
        with pytest.raises(RollbackRefused, match="need a workspace"):
            await rollback_scope(ledger, run_id)

    async def test_an_unknown_scope_rolls_back_nothing(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
    ) -> None:
        report = await rollback_scope(
            ledger,
            uuid.uuid4(),
            workspace_root=workspace,
            blob_store=blob_store,
        )
        assert report.entries == []
        assert report.exit_code == 1

    async def test_a_scope_with_no_effect_rows_rolls_back_nothing(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Any,
        builder: ScopeBuilder,
    ) -> None:
        # A real lineage with real calls, but no call declared an effect:
        # the honest report is "nothing to roll back", not an error.
        run_id = uuid.uuid4()
        journal = CallJournal(builder.ledger, run_id, blob_store=builder.blobs)
        await journal.run_started("demo.writer", "1.0.0")
        await journal.call_proposed(1, "fs.read", {"path": "out/a.txt"})
        await journal.call_completed(1, "fs.read", {"bytes": 0})

        report = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )
        assert report.entries == []
        assert report.exit_code == 1
