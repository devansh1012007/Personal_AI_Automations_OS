"""The generalised call journal's own contract (C6.2 / P2.1).

What these tests pin, in the house style of ``tests/ledger/test_replay.py``
and ``tests/chat/test_chat_events.py``:

* **fingerprint canonicality** — key order and whitespace cannot change a
  call's identity, because identity is the canonical JSON digest;
* **the golden v1 payload shapes** — every row carries ``v``, the run id,
  its seq, its method; params never enter a payload, only their sha and a
  shape-only preview; a FAILED row never attests a result;
* **idempotent redelivery** — a re-appended logical row dedupes in the
  store's own transaction (one event per discriminator), while one call's
  PROPOSED and COMPLETED stay distinct rows;
* **the replay property over all orderings** — the fold is total and
  seq-keyed over every permutation of a run's rows, and the records the
  fold returns are the same whatever order the ledger read produced;
* **the unknown outcome** — a PROPOSED with no outcome folds to
  ``PROPOSED`` (the state the D-6 rule classifies), never to a guess.
"""

from __future__ import annotations

import hashlib
import itertools
import uuid
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.core.types import EventType
from paa.ledger.events import LedgerEvent, canonical_json
from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import (
    CallJournal,
    CallOutcome,
    CallRecord,
    fingerprint,
    fold_call_journal,
    params_shape,
    read_call_journal,
)
from tests.conftest import RecordingRequeuer


@pytest.fixture
def run_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def journal(ledger: LedgerStore, run_id: uuid.UUID) -> CallJournal:
    return CallJournal(ledger, run_id)


async def test_fingerprint_is_canonical_over_key_order() -> None:
    """The same call in any key order is the same fingerprint."""
    a = fingerprint("think", {"role": "worker", "prompt": "hello"})
    b = fingerprint("think", {"prompt": "hello", "role": "worker"})
    assert a == b
    assert a == hashlib.sha256(
        canonical_json(
            {"method": "think", "params": {"role": "worker", "prompt": "hello"}}
        ).encode("utf-8")
    ).hexdigest()


def test_fingerprint_changes_when_params_change() -> None:
    assert fingerprint("think", {"prompt": "a"}) != fingerprint("think", {"prompt": "b"})
    assert fingerprint("think", {"prompt": "a"}) != fingerprint("recall", {"prompt": "a"})


def test_params_shape_never_carries_string_content() -> None:
    """Strings render as shapes — an opaque secret leaves only len+digest."""
    secret = "sk-live-abcdefghijklmnop"
    shape = params_shape({"prompt": secret, "k": 3})
    assert secret not in shape
    assert f"len={len(secret)}" in shape
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
    assert digest in shape
    assert "k=3" in shape


def test_params_shape_caps_long_previews() -> None:
    shape = params_shape({f"key_{i}": "x" for i in range(40)})
    assert len(shape) <= 240
    assert shape.endswith("…")


async def test_run_lifecycle_rows_are_the_golden_shape(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    started = await journal.run_started("coder.tool-loop", "4.1.0")
    assert started.payload["v"] == 1
    assert started.payload["pipeline"] == "coder.tool-loop"
    assert started.payload["version"] == "4.1.0"
    assert started.payload["run_id"] == str(run_id)
    assert started.discriminator == "run:STARTED"

    completed = await journal.run_completed("completed", metrics={"steps": 2})
    assert completed.event_type is EventType.PIPELINE_RUN_COMPLETED
    assert completed.payload["status"] == "completed"
    assert completed.payload["metrics"] == {"steps": 2}

    failed = await journal.run_failed("boom " * 100)
    assert failed.payload["reason"] == ("boom " * 100)[:200]


async def test_call_rows_are_the_golden_shape(
    journal: CallJournal, run_id: uuid.UUID
) -> None:
    proposed = await journal.call_proposed(1, "think", {"prompt": "hello"})
    assert proposed.event_type is EventType.PIPELINE_CALL_PROPOSED
    assert proposed.payload["v"] == 1
    assert proposed.payload["run_id"] == str(run_id)
    assert proposed.payload["seq"] == 1
    assert proposed.payload["method"] == "think"
    assert proposed.payload["fingerprint"] == fingerprint("think", {"prompt": "hello"})
    assert proposed.payload["params_shape"] == params_shape({"prompt": "hello"})
    assert "hello" not in proposed.payload["params_shape"]
    assert proposed.discriminator == "call:1:PROPOSED"

    result = {"text": "done", "tokens": 3}
    completed = await journal.call_completed(1, "think", result)
    assert completed.event_type is EventType.PIPELINE_CALL_COMPLETED
    assert completed.payload["result_json"] == canonical_json(result)
    expected_sha = hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()
    assert completed.payload["result_sha256"] == expected_sha
    assert completed.discriminator == "call:1:COMPLETED"

    failed = await journal.call_failed(2, "recall", "no such memory")
    assert failed.event_type is EventType.PIPELINE_CALL_FAILED
    assert failed.payload["reason"] == "no such memory"
    assert "result_sha256" not in failed.payload
    assert "result_json" not in failed.payload


async def test_divergence_row_names_the_offending_call(journal: CallJournal) -> None:
    row = await journal.run_diverged(3, "expected-fp", "actual-fp")
    assert row.event_type is EventType.PIPELINE_RUN_DIVERGED
    assert row.payload["seq"] == 3
    assert row.payload["expected_fingerprint"] == "expected-fp"
    assert row.payload["actual_fingerprint"] == "actual-fp"
    assert row.discriminator == "run:DIVERGED"


async def test_non_positive_seq_is_refused_before_any_append(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    with pytest.raises(ValueError, match="seq"):
        await journal.call_proposed(0, "think", {})
    events = await ledger.read_correlation(run_id)
    assert events == []


async def test_redelivered_append_dedupes_in_the_store(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    """One discriminator, one row — the store's transaction is the dedupe."""
    await journal.call_proposed(1, "think", {"prompt": "a"})
    await journal.call_proposed(1, "think", {"prompt": "a"})
    await journal.call_completed(1, "think", {"ok": True})
    await journal.call_completed(1, "think", {"ok": True})

    events = await ledger.read_correlation(run_id)
    kinds = [e.event_type for e in events]
    assert kinds.count(EventType.PIPELINE_CALL_PROPOSED) == 1
    assert kinds.count(EventType.PIPELINE_CALL_COMPLETED) == 1


async def test_fold_returns_records_in_seq_order(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    for seq in (3, 1, 2):
        await journal.call_proposed(seq, "think", {"prompt": f"p{seq}"})
        await journal.call_completed(seq, "think", {"seq": seq})

    records = await read_call_journal(ledger, run_id)
    assert [r.seq for r in records] == [1, 2, 3]
    for record in records:
        assert record.outcome is CallOutcome.COMPLETED
        assert record.result == {"seq": record.seq}
        assert record.method == "think"


async def test_fold_reads_the_unknown_outcome_as_proposed(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    """A crash between PROPOSED and the outcome leaves PROPOSED — no guess."""
    await journal.call_proposed(1, "think", {"prompt": "a"})
    await journal.call_completed(1, "think", {"ok": True})
    await journal.call_proposed(2, "think", {"prompt": "b"})

    records = await read_call_journal(ledger, run_id)
    assert records[0].outcome is CallOutcome.COMPLETED
    assert records[1].outcome is CallOutcome.PROPOSED
    assert records[1].result is None
    assert records[1].error is None


async def test_fold_is_total_over_malformed_rows(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    """Bad rows degrade, never raise; the well-formed rows still fold."""
    await ledger.append(
        LedgerEvent.create(
            run_id,
            EventType.PIPELINE_CALL_PROPOSED,
            attempt=0,
            discriminator="call:bad:PROPOSED",
            payload={"v": 1, "seq": "not-an-int", "method": "think"},
        )
    )
    await journal.call_proposed(1, "think", {"prompt": "a"})
    await ledger.append(
        LedgerEvent.create(
            run_id,
            EventType.PIPELINE_CALL_COMPLETED,
            attempt=0,
            discriminator="call:bad:COMPLETED",
            payload={"v": 1, "seq": True, "result_json": "{invalid"},
        )
    )

    records = await read_call_journal(ledger, run_id)
    assert [r.seq for r in records] == [1]
    assert records[0].outcome is CallOutcome.PROPOSED


async def test_replay_property_all_orderings(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    """The fold over every permutation of a run's rows is the same fold.

    The store orders rows by ``state_version``; this property pins the
    stronger claim the replay path leans on — whatever order a reader
    produces the rows in (a resharded store, a partial replay, a test
    double), the folded records are identical, because identity is the
    seq key, not arrival order. Includes the run-lifecycle rows: they
    interleave with call rows in every real lineage and the fold must
    ignore them, not trip on them.
    """
    await journal.run_started("coder.tool-loop", "4.1.0")
    await journal.call_proposed(1, "think", {"prompt": "a"})
    await journal.call_completed(1, "think", {"n": 1})
    await journal.call_proposed(2, "use_skill", {"skill": "bash", "args": {"cmd": "ls"}})
    await journal.call_failed(2, "use_skill", "permission denied")
    await journal.call_proposed(3, "recall", {"query": "x"})
    await journal.call_completed(3, "recall", {"facts": []})
    await journal.run_completed("completed", metrics={"calls": 3})

    events = await ledger.read_correlation(run_id)
    assert len(events) == 8  # the property below is over 8! orderings

    # The reference fold, keyed the way the store orders rows.
    reference = fold_call_journal(events)
    assert [(r.seq, r.outcome) for r in reference] == [
        (1, CallOutcome.COMPLETED),
        (2, CallOutcome.FAILED),
        (3, CallOutcome.COMPLETED),
    ]
    assert reference[1].error == "permission denied"

    for permutation in itertools.permutations(events):
        folded = fold_call_journal(list(permutation))
        assert [(r.seq, r.outcome) for r in folded] == [
            (1, CallOutcome.COMPLETED),
            (2, CallOutcome.FAILED),
            (3, CallOutcome.COMPLETED),
        ]
        assert folded[0].result == {"n": 1}
        assert folded[1].error == "permission denied"
        assert folded[2].result == {"facts": []}


async def test_fold_skips_foreign_lineage_events(journal: CallJournal) -> None:
    """A chat row or a task row in the input is not a call record."""
    await journal.call_proposed(1, "think", {"prompt": "a"})
    foreign = LedgerEvent.create(
        uuid.uuid4(),
        EventType.CHAT_TURN_PROPOSED,
        attempt=0,
        discriminator="t1:PROPOSED",
        payload={"v": 1, "turn_id": "t1"},
    )
    records = fold_call_journal([foreign])
    assert records == []


def test_call_record_is_frozen() -> None:
    record = CallRecord(seq=1, method="think", fingerprint="fp", outcome=CallOutcome.PROPOSED)
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises
        record.seq = 2  # type: ignore[misc]


async def test_oversized_result_blobs_through_the_store(
    ledger: LedgerStore, run_id: uuid.UUID, tmp_path: Any
) -> None:
    """A result past the inline limit rides the blob store, not the row."""
    from paa.storage.coldlake.cas import ContentAddressedStore

    store = ContentAddressedStore(tmp_path / "blobs")
    journal = CallJournal(ledger, run_id, blob_store=store)
    big = {"text": "x" * 20_000}
    completed = await journal.call_completed(1, "think", big)
    assert "result_json" not in completed.payload  # the stub carries the descriptor

    records = await read_call_journal(ledger, run_id, blob_store=store)
    assert records[0].outcome is CallOutcome.COMPLETED
    assert records[0].result == big
    assert records[0].result_sha256 == hashlib.sha256(
        canonical_json(big).encode("utf-8")
    ).hexdigest()


async def test_oversized_result_without_a_store_refuses(
    journal: CallJournal, run_id: uuid.UUID, ledger: LedgerStore
) -> None:
    with pytest.raises(ValueError, match="no blob store"):
        await journal.call_completed(1, "think", {"text": "x" * 20_000})
    events = await ledger.read_correlation(run_id)
    assert events == []


# ---------------------------------------------------------------------------
# Totality over arbitrary orderings (the house hypothesis rule, ADR-0050:
# "replay property tests over all orderings including the new types")
# ---------------------------------------------------------------------------


class TestFoldTotality:
    @given(
        st.lists(
            st.sampled_from(
                [
                    EventType.PIPELINE_RUN_STARTED,
                    EventType.PIPELINE_RUN_COMPLETED,
                    EventType.PIPELINE_RUN_FAILED,
                    EventType.PIPELINE_RUN_DIVERGED,
                    EventType.PIPELINE_CALL_PROPOSED,
                    EventType.PIPELINE_CALL_COMPLETED,
                    EventType.PIPELINE_CALL_FAILED,
                    # Foreign types ride real lineages and must be ignored.
                    EventType.TASK_REQUESTED,
                    EventType.CHAT_TURN_PROPOSED,
                    EventType.TOOL_CALL_COMPLETED,
                ]
            ),
            min_size=0,
            max_size=30,
        )
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_fold_never_raises_on_any_ordering(self, kinds: list[EventType]) -> None:
        """Total function: recovery meets malformed histories after crashes."""
        run = uuid.uuid4()
        events = [
            LedgerEvent(
                correlation_id=run,
                event_type=kind,
                state_version=i + 1,
                payload={},
            )
            for i, kind in enumerate(kinds)
        ]
        records = fold_call_journal(events)
        assert all(isinstance(r.seq, int) for r in records)
        # Empty payloads carry no usable seq, so nothing folds — the point
        # is that the call itself did not raise on any shape it met.
        assert records == []

    @given(
        st.lists(
            st.integers(min_value=1, max_value=6),
            min_size=0,
            max_size=12,
        ),
        st.randoms(use_true_random=False),
    )
    @hyp_settings(max_examples=150, deadline=None)
    def test_outcome_rows_beat_proposals_whatever_the_order(
        self, seqs: list[int], rnd: st.randoms
    ) -> None:
        """An outcome always resolves its proposal, in any arrival order."""
        run = uuid.uuid4()
        events: list[LedgerEvent] = []
        version = 0
        for seq in seqs:
            for kind in ("PROPOSED", "COMPLETED"):
                version += 1
                events.append(
                    LedgerEvent(
                        correlation_id=run,
                        event_type=(
                            EventType.PIPELINE_CALL_PROPOSED
                            if kind == "PROPOSED"
                            else EventType.PIPELINE_CALL_COMPLETED
                        ),
                        state_version=version,
                        payload={
                            "v": 1,
                            "seq": seq,
                            "method": "think",
                            "fingerprint": f"fp{seq}",
                            "result_json": '{"n": 1}',
                        },
                    )
                )
        rnd.shuffle(events)
        records = fold_call_journal(events)
        assert [r.seq for r in records] == sorted(set(seqs))
        assert all(r.outcome is CallOutcome.COMPLETED for r in records)


# ---------------------------------------------------------------------------
# The recovery sweep's half of the rule: a pipeline-only lineage is audit,
# never resumable task work (the chat exemption's mirror).
# ---------------------------------------------------------------------------


async def _run_lineage(
    ledger: LedgerStore, *, interrupted_at: int | None = None
) -> uuid.UUID:
    """One journalled run; ``interrupted_at`` leaves that seq outcome-less."""
    run = uuid.uuid4()
    journal = CallJournal(ledger, run)
    await journal.run_started("coder.tool-loop", "4.1.0")
    for seq in (1, 2, 3):
        await journal.call_proposed(seq, "think", {"prompt": f"p{seq}"})
        if interrupted_at is not None and seq == interrupted_at:
            continue  # the crash landed between PROPOSED and the outcome
        await journal.call_completed(seq, "think", {"n": seq})
    if interrupted_at is None:
        await journal.run_completed("completed", metrics={"calls": 3})
    return run


class TestRecoverySweepPipelineLineages:
    async def test_pipeline_only_lineage_is_skipped_never_requeued(
        self, ledger: LedgerStore, db, requeuer: RecordingRequeuer
    ) -> None:
        await _run_lineage(ledger)
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.scanned == 1
        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE) == 1
        assert requeuer.calls == []  # a run resumes by replay, never by requeue

    async def test_interrupted_call_is_closed_with_the_golden_shape(
        self, ledger: LedgerStore, db
    ) -> None:
        run = await _run_lineage(ledger, interrupted_at=2)
        report = await RecoveryEngine(ledger, db).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE) == 1
        events = await ledger.read_correlation(run)
        failed = [e for e in events if e.event_type is EventType.PIPELINE_CALL_FAILED]
        assert len(failed) == 1
        assert failed[0].payload == {
            "v": 1,
            "run_id": str(run),
            "seq": 2,
            "method": "think",
            "reason": "interrupted",
        }
        assert failed[0].discriminator == "call:2:FAILED"
        ok, problems = await ledger.verify_chain(run)
        assert ok, problems
        # The fold reads the close: a FAILED record, not an eternal proposal.
        records = await read_call_journal(ledger, run)
        assert [r.outcome for r in records] == [
            CallOutcome.COMPLETED,
            CallOutcome.FAILED,
            CallOutcome.COMPLETED,
        ]

    async def test_second_sweep_is_a_noop(self, ledger: LedgerStore, db) -> None:
        await _run_lineage(ledger, interrupted_at=1)
        engine = RecoveryEngine(ledger, db)
        await engine.boot_sweep()
        count_after_first = await ledger.count()

        report = await engine.boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE) == 1
        assert await ledger.count() == count_after_first

    async def test_task_lineage_with_pipeline_rows_stays_a_task(
        self, ledger: LedgerStore, db, requeuer: RecordingRequeuer
    ) -> None:
        """A lineage mixing TASK_* and PIPELINE_* rows recovers normally."""
        run = uuid.uuid4()
        await ledger.append(
            LedgerEvent.create(
                run,
                EventType.TASK_REQUESTED,
                attempt=0,
                discriminator="t",
                payload={"request": {"goal": "g"}},
            )
        )
        journal = CallJournal(ledger, run)
        await journal.call_proposed(1, "think", {"prompt": "a"})
        await journal.call_completed(1, "think", {"n": 1})

        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE) == 0
        # The ordinary path decided (resumed or parked, never skipped).
        assert (
            report.count(RecoveryOutcome.RESUMED)
            + report.count(RecoveryOutcome.PARKED_HUMAN)
            + report.count(RecoveryOutcome.ABANDONED)
            + report.count(RecoveryOutcome.ROLLED_BACK)
            >= 1
        )
