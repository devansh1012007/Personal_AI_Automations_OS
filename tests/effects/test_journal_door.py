"""The CallJournal's effect door (ADR-0054): riding the existing vocabulary.

The door is on ``PIPELINE_CALL_COMPLETED``, deliberately — no new
``EventType``, so the v=1 goldens, the ADR-0008 discriminator scheme,
the replay-total fold and the boot-sweep exemptions keep holding without
ceremony. What these tests pin: the ``effect`` and ``compensates`` fields
appear on the payload only when given, the fold is unchanged by them, and
an effect whose owner is not the call it rides aborts *before* anything
is appended (the same abort rule as a non-monotonic seq).
"""

from __future__ import annotations

import uuid

import pytest

from paa.effects.spec import (
    CompensationAction,
    CompensationSpec,
    EffectKind,
    EffectSpec,
    effect_id_for,
)
from paa.pipelines.journal import CallJournal, CallOutcome, fold_call_journal


@pytest.fixture
def run_id() -> uuid.UUID:
    return uuid.uuid4()


async def test_a_call_without_an_effect_writes_the_payload_it_always_wrote(
    ledger, run_id: uuid.UUID
) -> None:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("demo.writer", "1.0.0")
    await journal.call_proposed(1, "fs.write", {"path": "a.txt"})
    await journal.call_completed(1, "fs.write", {"written": "a.txt"})

    [row] = [e for e in await ledger.read_correlation(run_id)
             if e.event_type.value == "PIPELINE_CALL_COMPLETED"]
    assert "effect" not in row.payload
    assert "compensates" not in row.payload
    # The v=1 shape the goldens pin is unchanged by the door's existence.
    assert set(row.payload) == {
        "v", "run_id", "seq", "method", "result_sha256", "result_json",
    }


async def test_an_effect_rides_the_completed_row(
    ledger, run_id: uuid.UUID
) -> None:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("demo.writer", "1.0.0")
    effect = EffectSpec.for_call(
        run_id, 2, EffectKind.FILE_WRITE,
        CompensationSpec(action=CompensationAction.REMOVE, path="out/new.txt"),
    )
    await journal.call_proposed(2, "fs.write", {"path": "out/new.txt"})
    await journal.call_completed(2, "fs.write", {"written": True}, effect=effect)

    [row] = [e for e in await ledger.read_correlation(run_id)
             if e.event_type.value == "PIPELINE_CALL_COMPLETED"]
    assert row.payload["effect"] == effect.to_payload()
    assert row.payload["effect"]["v"] == 1


async def test_a_compensates_reference_rides_the_completed_row(
    ledger, run_id: uuid.UUID
) -> None:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("effects.rollback", "1.0.0", run_kind="rollback")
    target = effect_id_for(uuid.uuid4(), 1)
    await journal.call_proposed(1, "effects.compensate", {"effect_id": target})
    await journal.call_completed(
        1, "effects.compensate", {"outcome": "restored"}, compensates=target
    )

    [row] = [e for e in await ledger.read_correlation(run_id)
             if e.event_type.value == "PIPELINE_CALL_COMPLETED"]
    assert row.payload["compensates"] == target
    assert "effect" not in row.payload


async def test_the_fold_ignores_the_new_fields(ledger, run_id: uuid.UUID) -> None:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("demo.writer", "1.0.0")
    effect = EffectSpec.for_call(run_id, 1, EffectKind.IRREVERSIBLE)
    await journal.call_proposed(1, "mail.send", {"to": "world"})
    await journal.call_completed(
        1, "mail.send", {"sent": True}, effect=effect, compensates="x" * 64
    )

    records = fold_call_journal(await ledger.read_correlation(run_id))
    assert len(records) == 1
    assert records[0].outcome is CallOutcome.COMPLETED
    assert records[0].result == {"sent": True}


async def test_an_effect_ridden_by_the_wrong_call_aborts_before_appending(
    ledger, run_id: uuid.UUID
) -> None:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("demo.writer", "1.0.0")
    foreign = EffectSpec.for_call(
        uuid.uuid4(), 9, EffectKind.FILE_WRITE,
        CompensationSpec(action=CompensationAction.REMOVE, path="a.txt"),
    )
    await journal.call_proposed(1, "fs.write", {"path": "a.txt"})
    with pytest.raises(ValueError, match="owner must be the call it rides"):
        await journal.call_completed(1, "fs.write", {"written": True}, effect=foreign)
    # The abort rule: nothing was appended — the lineage holds exactly
    # the two rows the call had produced before the refusal.
    assert len(await ledger.read_correlation(run_id)) == 2


async def test_a_seq_mismatch_on_the_owner_aborts_too(ledger, run_id: uuid.UUID) -> None:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("demo.writer", "1.0.0")
    wrong_seq = EffectSpec.for_call(
        run_id, 5, EffectKind.FILE_WRITE,
        CompensationSpec(action=CompensationAction.REMOVE, path="a.txt"),
    )
    with pytest.raises(ValueError, match="owner must be the call it rides"):
        await journal.call_completed(1, "fs.write", {"written": True}, effect=wrong_seq)
