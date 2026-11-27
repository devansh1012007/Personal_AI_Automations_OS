"""The C9.2 chaos drill: kill -9 mid-rollback, sweep, re-run, no dupes.

The C6.3 matrix's discipline applied to the rollback walk. One scope with
three file effects — a creation, a modification of a pre-existing file, a
creation — and four kill points, one per boundary a mid-walk ``kill -9``
can actually reach:

* ``after-work-1`` / ``after-work-2`` / ``after-work-3`` — the process
  dies *inside* the nth walked effect, after the file work and before
  the ``PIPELINE_CALL_COMPLETED`` row (the exact unknown-outcome state:
  the workspace already holds the compensated bytes, the ledger holds a
  PROPOSED row and no settlement);
* ``before-work-2`` — the process dies after the second effect's
  PROPOSED row and before any file work (the effect never happened).

After every kill the suite runs the production startup sweep
(:func:`close_interrupted_pipeline_calls`) — twice, because a sweep that
is not idempotent would itself be a duplicate-effect source — then
re-runs the rollback *clean* (a fresh derivation from the ledger, which
is what a rebooted process does; the registry holds no state a replay of
the ledger cannot rebuild) and pins the invariants:

* the workspace converges on the pre-run state — creations gone, the
  modified file holding its prior bytes;
* **each effect is settled exactly once** across kill + sweep + re-run
  (one ``compensates`` row per effect id, over every lineage — the
  property, not the aspiration);
* the re-run's report is honest about what was already settled;
* the killed call's dangling PROPOSED row is closed as ``interrupted``
  and reads FAILED in the fold (the conservative precedence), and stays
  closed;
* both the scope's lineage and the rollback lineages keep intact hash
  chains — the sweep appended rows, it did not rewrite any.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from paa.core.types import EventType
from paa.effects.rollback import rollback_scope
from paa.ledger.recovery import (
    close_interrupted_pipeline_calls,
    find_interrupted_pipeline_calls,
)
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import CallOutcome
from paa.pipelines.replay import ReplayView

from .conftest import EPOCH, FakeBlobStore, ScopeBuilder, all_compensates_refs


class Kill9(BaseException):
    """A hard kill, in process: nothing catches it, no cleanup row is
    written, the run simply stops."""


def _inject_kill(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kill_after_apply: int | None,
    kill_before_capture: int | None,
) -> None:
    """Patch the rollback walk's two file-work seams with counting kills.

    The seams are the module-level names the walk calls
    (``paa.effects.rollback.apply_compensation`` /
    ``capture_prior``), so the kill lands at the exact boundaries a real
    SIGKILL reaches: after the file work but before the completion row,
    or after PROPOSED but before the file work. The patches are scoped
    to the killed run only — the re-run walks clean.
    """
    import paa.effects.rollback as rollback_module

    state = {"applies": 0, "captures": 0}
    real_apply = rollback_module.apply_compensation
    real_capture = rollback_module.capture_prior

    def apply(root: Path, spec: Any, store: Any) -> Any:
        state["applies"] += 1
        outcome = real_apply(root, spec, store)
        if kill_after_apply is not None and state["applies"] == kill_after_apply:
            raise Kill9("simulated SIGKILL after the file work, before the row")
        return outcome

    def capture(root: Path, rel_path: str, store: Any) -> Any:
        state["captures"] += 1
        if kill_before_capture is not None and state["captures"] == kill_before_capture:
            raise Kill9("simulated SIGKILL after PROPOSED, before the file work")
        return real_capture(root, rel_path, store)

    monkeypatch.setattr(rollback_module, "apply_compensation", apply)
    monkeypatch.setattr(rollback_module, "capture_prior", capture)


async def _rollback_lineages(ledger: LedgerStore) -> list[uuid.UUID]:
    """Every rollback run lineage in the store, in ledger order.

    How a rebooted operator finds the killed run: the RUN_STARTED rows
    whose ``run_kind`` is ``rollback``, read back from the store alone.
    """
    rows = await ledger.events_since(
        EPOCH, event_types=(EventType.PIPELINE_RUN_STARTED,), limit=10_000
    )
    return [
        e.correlation_id
        for e in rows
        if e.payload.get("run_kind") == "rollback"
        and isinstance(e.correlation_id, uuid.UUID)
    ]


class TestKillAtEveryBoundary:
    """The matrix: one crash point per reachable walk boundary."""

    @pytest.mark.parametrize(
        "kill_at",
        ["after-work-1", "after-work-2", "after-work-3", "before-work-2"],
    )
    async def test_kill_sweep_re_run_settles_every_effect_exactly_once(
        self,
        ledger: LedgerStore,
        blob_store: FakeBlobStore,
        workspace: Path,
        builder: ScopeBuilder,
        kill_at: str,
    ) -> None:
        run_id = uuid.uuid4()
        # Pre-run state: b.txt exists; the scope's seq-2 effect modifies
        # it, so one walked compensation is a restore (bytes by address)
        # and the others are removes — both halves of the v1 surface.
        out = workspace / "out"
        out.mkdir()
        (out / "b.txt").write_bytes(b"b-zero")
        a = await builder.write_file(run_id, 1, "out/a.txt", "alpha")
        b = await builder.write_file(run_id, 2, "out/b.txt", "b-one")
        c = await builder.write_file(run_id, 3, "out/c.txt", "gamma")

        kill_after = {"after-work-1": 1, "after-work-2": 2, "after-work-3": 3}.get(
            kill_at
        )
        kill_before = {"before-work-2": 2}.get(kill_at)

        # -- run A: walk until the kill --------------------------------
        with pytest.MonkeyPatch.context() as mp:
            _inject_kill(
                mp, kill_after_apply=kill_after, kill_before_capture=kill_before
            )
            with pytest.raises(Kill9):
                await rollback_scope(
                    ledger, run_id, workspace_root=workspace, blob_store=blob_store
                )

        # -- reopen the store: the production startup sweep, twice -----
        [killed] = await _rollback_lineages(ledger)
        events = await ledger.read_correlation(killed)
        interrupted = find_interrupted_pipeline_calls(events)
        # The kill left exactly one call dangling: the one in flight.
        assert len(interrupted) == 1
        await close_interrupted_pipeline_calls(ledger, interrupted)
        await close_interrupted_pipeline_calls(ledger, interrupted)  # idempotent

        # -- run B: re-run clean, a fresh derivation -------------------
        report = await rollback_scope(
            ledger, run_id, workspace_root=workspace, blob_store=blob_store
        )

        # -- the invariants --------------------------------------------
        # The world has converged on the pre-run state.
        assert not (workspace / "out/a.txt").exists()
        assert (workspace / "out/b.txt").read_bytes() == b"b-zero"
        assert not (workspace / "out/c.txt").exists()

        # No double compensation: each effect settled exactly once, over
        # every lineage the kill and the re-run produced.
        assert sorted(await all_compensates_refs(ledger)) == sorted(
            [a.effect_id, b.effect_id, c.effect_id]
        )

        # The re-run's report is honest: everything either newly
        # compensated or already settled, and the run did work (the
        # kill always left at least one effect unsettled).
        assert report.exit_code == 0
        assert report.failed == 0
        assert report.newly_compensated + report.already_compensated == 3

        # The swept call stays closed and reads FAILED in the fold (the
        # conservative precedence — the settlement lives on a *different*
        # lineage, the re-run's, and this one must not resurrect).
        swept = await ReplayView.load(ledger, killed)
        dangling_seq = interrupted[0].payload["seq"]
        record = swept.record_at(dangling_seq)
        assert record is not None
        assert record.outcome is CallOutcome.FAILED
        assert record.error == "interrupted"
        assert find_interrupted_pipeline_calls(
            await ledger.read_correlation(killed)
        ) == []

        # The chains held: appending rows (the sweep's, the re-run's)
        # never rewrote any.
        for lineage in [run_id, killed, *await _rollback_lineages(ledger)]:
            ok, problems = await ledger.verify_chain(lineage)
            assert ok, problems
