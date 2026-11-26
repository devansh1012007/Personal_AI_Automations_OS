"""W8 C8.6: the two pipeline-package event types (ADR-0046).

What is pinned here — the same contract the extension family pins in its
own file, because the pattern is the point of ADR-0046:

* The v1 payload contract of both types (golden dicts), including the
  capability review's decision block — the C8.1 human-gate artifact's
  own vocabulary, riding inside the install record — and the conformance
  verdicts the decision was made on.
* The idempotency discriminators ``install``/``refused`` fit the
  ADR-0008 key, and an install and a refusal are distinct rows while a
  redelivered append dedupes.
* Phase inertness: both in ``_NO_PHASE_CHANGE``, neither in
  ``_PHASE_MAP``, neither in ``TERMINAL_EVENTS``.
* The tier fold ignores them (no ``fact_id`` in any payload).
* Store round-trip: both types read back through the store and the hash
  chain verifies over one install lineage.
* The boot sweep reads a pipeline-package-only lineage as
  ``SKIPPED_PIPELINE_PACKAGE_LINEAGE``: an install is re-driven by the
  operator running the installer again, never re-queued as task work —
  the fifth mirror of the extension surface's rule.
"""

from __future__ import annotations

import json
import uuid

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.core.types import TERMINAL_EVENTS, EventType, new_correlation_id
from paa.ledger.events import LedgerEvent, canonical_json, compute_idempotency_key
from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome
from paa.ledger.replay import _NO_PHASE_CHANGE, _PHASE_MAP, apply_tier_event, replay
from paa.ledger.store import LedgerStore
from paa.storage.relational.database import Database

PIPELINE_PACKAGE_TYPES = (
    EventType.PIPELINE_PACKAGE_INSTALLED,
    EventType.PIPELINE_PACKAGE_REFUSED,
)

# -- golden v1 payloads (ADR-0046) -----------------------------------------
# The frozen contracts. Every payload carries `v` = 1; a version bump
# requires updating these rows in the same commit (the ADR-0046 §Forward-
# compatibility tripwire). The `lineage` value names the install action's
# own fresh lineage — the same uuid the correlation carries, restated in
# the payload so `read_correlation(<lineage>)` is the whole story. The
# gates list and the review/conformance blocks are the shapes
# `paa.pipelines.packages` journals; the source vocabulary is the P5-lite
# set {local_dir, git_pinned, paapkg}.

GOLDEN_PAYLOADS: dict[EventType, dict] = {
    EventType.PIPELINE_PACKAGE_INSTALLED: {
        "v": 1,
        "lineage": "11111111-2222-3333-4444-555555555555",
        "package_name": "demo.echo",
        "version": "1.2.3",
        "source": "git_pinned",
        "source_ref": "f" * 40,
        "content_hash": "a" * 64,
        "gates": ["signature", "content_hash", "ast_scan", "permissions", "smoke_test"],
        "signature_state": "unverified",
        "capabilities": {"think": {"roles": ["worker"]}},
        "review": {
            "decision": "approved",
            "approver_kind": "human",
            "approver_id": "operator",
            "note": "the fixture ask",
            "recorded_at": "2026-11-26T00:00:00+00:00",
        },
        "conformance": {
            "double-run-sequence-equality": "pass",
            "divergence-halt": "pass",
            "unknown-outcome-never-auto-repeats": "pass",
            "cancel-within-grace": "pass",
            "budget-exhaustion": "pass",
            "no-secrets-in-journal": "pass",
        },
    },
    EventType.PIPELINE_PACKAGE_REFUSED: {
        "v": 1,
        "lineage": "66666666-7777-8888-9999-aaaaaaaaaaaa",
        "package_name": "demo.echo",
        "version": "1.2.3",
        "source": "local_dir",
        "stage": "capability_review",
        "reason": "the capability review was refused by human:operator",
    },
}

# The two discriminators the package journal mints: package- and
# stage-qualified, so one install action's rows stay distinct while a
# redelivered append dedupes under the ADR-0008 key.
_DISCRIMINATORS = {
    EventType.PIPELINE_PACKAGE_INSTALLED: "install:demo.echo:1.2.3",
    EventType.PIPELINE_PACKAGE_REFUSED: "refused:demo.echo:capability_review",
}

# The closed vocabularies the payloads ride (plan §9 rule 3: identifiers
# are closed-vocabulary or uuids, never prose). `source` is the P5-lite
# source set; `stage` is the flow's stages in order — the installer's own
# five gate names plus the four this flow adds around them.
_SOURCES = frozenset({"local_dir", "git_pinned", "paapkg"})
_STAGES = frozenset(
    {
        "source",
        "declaration",
        "signature",
        "content_hash",
        "ast_scan",
        "permissions",
        "smoke_test",
        "capability_review",
        "conformance",
    }
)


def _event(etype: EventType, cid: uuid.UUID, ver: int, **payload) -> LedgerEvent:
    return LedgerEvent(
        correlation_id=cid, event_type=etype, state_version=ver, payload=dict(payload)
    )


def _gold(etype: EventType, cid: uuid.UUID, version: int) -> LedgerEvent:
    """An event carrying its golden v1 payload (keeps long lines short)."""
    return _event(etype, cid, version, **GOLDEN_PAYLOADS[etype])


class TestGoldenV1Payloads:
    """Each of the 2 types carries its exact v1 payload, byte-stable."""

    def test_two_golden_payloads_are_pinned(self) -> None:
        assert set(GOLDEN_PAYLOADS) == set(PIPELINE_PACKAGE_TYPES)
        assert len(GOLDEN_PAYLOADS) == 2

    def test_every_payload_carries_v1(self) -> None:
        for etype, payload in GOLDEN_PAYLOADS.items():
            assert payload["v"] == 1, f"{etype.value} lost its schema version"

    def test_payload_round_trips_through_canonical_json(self) -> None:
        """canonical_json is frozen — reordered keys produce identical bytes."""
        for payload in GOLDEN_PAYLOADS.values():
            reordered = dict(reversed(list(payload.items())))
            assert canonical_json(payload) == canonical_json(reordered)
            assert json.loads(canonical_json(payload)) == payload

    def test_v1_payloads_carry_no_fact_id(self) -> None:
        """Package payloads never name a fact; the tier fold must ignore them."""
        for etype, payload in GOLDEN_PAYLOADS.items():
            assert "fact_id" not in payload, f"{etype.value} would leak into tier state"

    def test_sources_and_stages_are_closed_vocabularies(self) -> None:
        """`source` and `stage` are classifiers, not free text."""
        for etype in PIPELINE_PACKAGE_TYPES:
            assert GOLDEN_PAYLOADS[etype]["source"] in _SOURCES
        assert GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_REFUSED]["stage"] in _STAGES

    def test_the_review_block_is_the_human_gate_vocabulary(self) -> None:
        """The C8.1 artifact's own shape rides inside the install record."""
        review = GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_INSTALLED]["review"]
        assert set(review) == {
            "decision",
            "approver_kind",
            "approver_id",
            "note",
            "recorded_at",
        }
        assert review["decision"] in ("approved", "rejected")
        assert review["approver_kind"] and review["approver_id"]

    def test_the_conformance_block_names_the_six_checks(self) -> None:
        conformance = GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_INSTALLED]["conformance"]
        assert len(conformance) == 6
        assert set(conformance.values()) == {"pass"}

    def test_refusal_reason_is_bounded(self) -> None:
        """The journal truncates at 200 chars; the golden respects its own rule."""
        reason = GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_REFUSED]["reason"]
        assert isinstance(reason, str)
        assert len(reason) <= 200


class TestIdempotencyDiscriminator:
    """The `install`/`refused` discriminators fit the ADR-0008 key."""

    def test_key_shape_matches_adr0008(self) -> None:
        cid = new_correlation_id()
        etype = EventType.PIPELINE_PACKAGE_INSTALLED
        event = _event(etype, cid, 1, **GOLDEN_PAYLOADS[etype])
        event = event.model_copy(update={"discriminator": _DISCRIMINATORS[etype]})
        assert event.idempotency_key == compute_idempotency_key(
            cid, etype, attempt=0, discriminator=_DISCRIMINATORS[etype]
        )

    def test_install_and_refusal_are_distinct_rows(self) -> None:
        """An install and its refusal could never share a key."""
        cid = new_correlation_id()
        keys = {
            compute_idempotency_key(
                cid,
                etype,
                attempt=0,
                discriminator=_DISCRIMINATORS[etype],
            )
            for etype in PIPELINE_PACKAGE_TYPES
        }
        assert len(keys) == 2

    async def test_redelivery_dedupes_but_attempt_does_not(self, ledger: LedgerStore) -> None:
        """Same discriminator suppresses; a bumped attempt is a new event."""
        cid = new_correlation_id()
        etype = EventType.PIPELINE_PACKAGE_INSTALLED
        payload = dict(GOLDEN_PAYLOADS[etype])
        first = await ledger.append(
            LedgerEvent.create(
                cid, etype, payload=payload, discriminator=_DISCRIMINATORS[etype]
            )
        )
        second = await ledger.append(
            LedgerEvent.create(
                cid, etype, payload=payload, discriminator=_DISCRIMINATORS[etype]
            )
        )
        assert second.event_id == first.event_id
        assert await ledger.count() == 1
        key = compute_idempotency_key(
            cid, etype, attempt=0, discriminator=_DISCRIMINATORS[etype]
        )
        assert key != compute_idempotency_key(
            cid, etype, attempt=1, discriminator=_DISCRIMINATORS[etype]
        )


class TestPhaseInertness:
    """Package events move no phase, close nothing, and are never unknown."""

    def test_both_are_in_no_phase_change(self) -> None:
        for etype in PIPELINE_PACKAGE_TYPES:
            assert etype in _NO_PHASE_CHANGE, f"{etype.value} must be informational"
            assert etype not in _PHASE_MAP, f"{etype.value} must not move a phase"

    def test_neither_is_terminal(self) -> None:
        for etype in PIPELINE_PACKAGE_TYPES:
            assert etype not in TERMINAL_EVENTS, f"{etype.value} must never close a lineage"

    def test_package_events_interleaved_in_a_task_do_not_perturb_it(self) -> None:
        cid = new_correlation_id()
        task = [
            _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
            _event(EventType.EXECUTION_STARTED, cid, 2, step_index=0),
            _event(EventType.EXECUTION_COMPLETED, cid, 3, step_index=0, tokens_consumed=10),
            _event(EventType.MUTATION_COMMITTED, cid, 4, applied_patch_sha256="a" * 64),
        ]
        interleaved = [
            _gold(EventType.PIPELINE_PACKAGE_INSTALLED, cid, 5),
        ]
        full = replay(task)
        mixed = replay(task[:2] + interleaved + task[2:])
        assert mixed.phase is full.phase
        assert mixed.unknown_events == []
        assert mixed.completed_steps == full.completed_steps
        assert mixed.tokens_consumed == full.tokens_consumed

    @given(
        seq=st.lists(st.sampled_from(list(PIPELINE_PACKAGE_TYPES)), min_size=1, max_size=10)
    )
    @hyp_settings(max_examples=60, deadline=None)
    def test_replay_total_over_package_orderings(self, seq: list[EventType]) -> None:
        """Replay is total over every ordering of the two types: no
        ordering records an unknown event, moves a phase, or reads a
        package-only lineage as task work with attempts."""
        cid = uuid.uuid4()
        events = [_event(et, cid, i + 1, **GOLDEN_PAYLOADS[et]) for i, et in enumerate(seq)]
        state = replay(events)
        assert state.unknown_events == []
        assert state.attempts == 0
        assert not state.is_terminal


class TestTierFoldIgnoresPackageEvents:
    def test_apply_tier_event_is_a_noop_for_both(self) -> None:
        cid = new_correlation_id()
        state: dict[str, str] = {}
        for i, etype in enumerate(PIPELINE_PACKAGE_TYPES):
            folded = apply_tier_event(
                state, _event(etype, cid, i + 1, **GOLDEN_PAYLOADS[etype])
            )
            assert folded == state == {}


class TestStoreRoundTrip:
    async def test_an_install_lineage_round_trips_and_chains(
        self, ledger: LedgerStore
    ) -> None:
        """The shape one real install writes: one row on the install
        action's lineage, in ledger order."""
        cid = new_correlation_id()
        installed = dict(GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_INSTALLED])
        # The payload's `lineage` restates the correlation id it rides.
        installed["lineage"] = str(cid)
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.PIPELINE_PACKAGE_INSTALLED,
                payload=installed,
                discriminator=_DISCRIMINATORS[EventType.PIPELINE_PACKAGE_INSTALLED],
            )
        )
        rows = await ledger.read_correlation(cid)
        assert [row.event_type for row in rows] == [EventType.PIPELINE_PACKAGE_INSTALLED]
        assert [row.payload for row in rows] == [installed]
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems
        head = await ledger.head(cid)
        assert head is not None
        assert not head.is_terminal  # the head must not close an install lineage

    async def test_a_refusal_lineage_round_trips_too(self, ledger: LedgerStore) -> None:
        """A refused install is the whole story of what was tried: one
        REFUSED row, readable the same way the install's row reads."""
        cid = new_correlation_id()
        refused = dict(GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_REFUSED])
        refused["lineage"] = str(cid)
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.PIPELINE_PACKAGE_REFUSED,
                payload=refused,
                discriminator=_DISCRIMINATORS[EventType.PIPELINE_PACKAGE_REFUSED],
            )
        )
        rows = await ledger.read_correlation(cid)
        assert [row.event_type for row in rows] == [EventType.PIPELINE_PACKAGE_REFUSED]
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems


class TestRecoverySweepSkipsPackageLineages:
    """An install is an operator action, never resumable task work."""

    async def test_package_only_lineage_is_skipped_never_requeued(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        from tests.conftest import RecordingRequeuer

        lineages: dict[EventType, uuid.UUID] = {}
        for etype in PIPELINE_PACKAGE_TYPES:
            cid = new_correlation_id()
            lineages[etype] = cid
            payload = dict(GOLDEN_PAYLOADS[etype])
            payload["lineage"] = str(cid)
            await ledger.append(
                LedgerEvent.create(
                    cid, etype, payload=payload, discriminator=_DISCRIMINATORS[etype]
                )
            )

        requeuer = RecordingRequeuer()
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.scanned == 2
        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_PACKAGE_LINEAGE) == 2
        assert requeuer.calls == []  # an install is never re-queued as a task
        # The sweep wrote nothing: the flow is atomic — a refusal at any
        # stage leaves zero trace — so each lineage is exactly what it was.
        for etype, cid in lineages.items():
            rows = await ledger.read_correlation(cid)
            assert [e.event_type for e in rows] == [etype]

    async def test_a_stale_recovery_row_does_not_flatten_a_package_lineage(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        """RECOVERY_REPLAY_COMPLETED is neutral to the package-only
        determination, exactly as it is to the extension's."""
        from tests.conftest import RecordingRequeuer

        cid = new_correlation_id()
        payload = dict(GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_REFUSED])
        payload["lineage"] = str(cid)
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.PIPELINE_PACKAGE_REFUSED,
                payload=payload,
                discriminator=_DISCRIMINATORS[EventType.PIPELINE_PACKAGE_REFUSED],
            )
        )
        await ledger.append(
            LedgerEvent.create(
                cid, EventType.RECOVERY_REPLAY_COMPLETED, payload={"recovered": 0}
            )
        )

        requeuer = RecordingRequeuer()
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_PACKAGE_LINEAGE) == 1
        assert requeuer.calls == []

    async def test_package_events_do_not_flatten_a_task_lineage(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        """A lineage mixing TASK_* rows with package rows is a task
        lineage the ordinary path recovers; the phase-inert package
        rows disturb nothing there."""
        from tests.conftest import RecordingRequeuer

        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "x"}})
        )
        await ledger.append(
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.PIPELINE_PACKAGE_INSTALLED,
                payload=dict(GOLDEN_PAYLOADS[EventType.PIPELINE_PACKAGE_INSTALLED]),
                discriminator=_DISCRIMINATORS[EventType.PIPELINE_PACKAGE_INSTALLED],
            )
        )

        requeuer = RecordingRequeuer()
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_PACKAGE_LINEAGE) == 0
        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
