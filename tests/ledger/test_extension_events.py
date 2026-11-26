"""W8 C8.4: the three chat-extension event types (ADR-0046).

What is pinned here — the same contract the coder/chat/thread families
pin in their own files, because the pattern is the point of ADR-0046:

* The v1 payload contract of all three types (golden dicts), including
  ``EXTENSION_ACTIVATED``'s ``pipeline`` — the coding loop the
  activation selected through the pipeline registry, which is what makes
  "CODING_FALLBACK_PIPELINE was selected here" a checkable fact rather
  than a docstring claim.
* The idempotency discriminators ``install/refuse/activate`` fit the
  ADR-0008 key, and an install and its activation are distinct rows on
  one lineage while a redelivered append dedupes.
* Phase inertness: all three in ``_NO_PHASE_CHANGE``, none in
  ``_PHASE_MAP``, none in ``TERMINAL_EVENTS``.
* The tier fold ignores them (no ``fact_id`` in any payload).
* Store round-trip: the three types read back through the store and the
  hash chain verifies over an install lineage.
* The boot sweep reads an extension-only lineage as
  ``SKIPPED_EXTENSION_LINEAGE``: an install is re-driven by the operator
  running the installer again, never re-queued as task work.
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

EXTENSION_TYPES = (
    EventType.EXTENSION_INSTALLED,
    EventType.EXTENSION_REFUSED,
    EventType.EXTENSION_ACTIVATED,
)

# -- golden v1 payloads (ADR-0046) -----------------------------------------
# The frozen contracts. Every payload carries `v` = 1; a version bump
# requires updating these rows in the same commit (the ADR-0046 §Forward-
# compatibility tripwire). The `lineage` value names the install action's
# own fresh lineage — the same uuid the correlation carries, restated in
# the payload so `read_correlation(<lineage>)` is the whole story.

GOLDEN_PAYLOADS: dict[EventType, dict] = {
    EventType.EXTENSION_INSTALLED: {
        "v": 1,
        "lineage": "11111111-2222-3333-4444-555555555555",
        "package_name": "greeter",
        "version": "1.0.0",
        "kind": "skill",
        "source": "claw_hub",
        "content_hash": "a" * 64,
        "gates": ["signature", "content_hash", "ast_scan", "permissions", "smoke_test"],
        "signature_state": "unverified",
    },
    EventType.EXTENSION_REFUSED: {
        "v": 1,
        "lineage": "66666666-7777-8888-9999-aaaaaaaaaaaa",
        "package_name": "evil",
        "version": "0.9.0",
        "kind": "skill",
        "source": "personal",
        "gate": "ast_scan",
        "reason": "AST scan rejected evil.py: os.system is blocked",
    },
    EventType.EXTENSION_ACTIVATED: {
        "v": 1,
        "lineage": "11111111-2222-3333-4444-555555555555",
        "skill_name": "greeter",
        "version": "1.0.0",
        "provider": "claw_hub",
        "source": "claw_hub",
        "pipeline": "coder.tool-loop",
    },
}

# The three discriminators the ExtensionJournal mints: package- and
# version-qualified, so one install action's rows stay distinct while a
# redelivered append dedupes under the ADR-0008 key.
_DISCRIMINATORS = {
    EventType.EXTENSION_INSTALLED: "install:greeter:1.0.0",
    EventType.EXTENSION_REFUSED: "refused:evil:ast_scan",
    EventType.EXTENSION_ACTIVATED: "activate:greeter:1.0.0",
}


def _event(etype: EventType, cid: uuid.UUID, ver: int, **payload) -> LedgerEvent:
    return LedgerEvent(
        correlation_id=cid, event_type=etype, state_version=ver, payload=dict(payload)
    )


def _gold(etype: EventType, cid: uuid.UUID, version: int) -> LedgerEvent:
    """An event carrying its golden v1 payload (keeps long lines short)."""
    return _event(etype, cid, version, **GOLDEN_PAYLOADS[etype])


class TestGoldenV1Payloads:
    """Each of the 3 types carries its exact v1 payload, byte-stable."""

    def test_three_golden_payloads_are_pinned(self) -> None:
        assert set(GOLDEN_PAYLOADS) == set(EXTENSION_TYPES)
        assert len(GOLDEN_PAYLOADS) == 3

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
        """Extension payloads never name a fact; the tier fold must ignore them."""
        for etype, payload in GOLDEN_PAYLOADS.items():
            assert "fact_id" not in payload, f"{etype.value} would leak into tier state"

    def test_sources_and_pipelines_are_closed_vocabularies(self) -> None:
        """`source` is a classifier, not free text; `pipeline` names a
        registered pipeline or is absent — the activation's own rule."""
        sources = {"claw_hub", "personal", "mcp_server"}
        for etype in (EventType.EXTENSION_INSTALLED, EventType.EXTENSION_REFUSED):
            assert GOLDEN_PAYLOADS[etype]["source"] in sources
        assert GOLDEN_PAYLOADS[EventType.EXTENSION_ACTIVATED]["source"] in sources
        activated = GOLDEN_PAYLOADS[EventType.EXTENSION_ACTIVATED]
        assert activated["pipeline"] == "coder.tool-loop"  # CODING_FALLBACK_PIPELINE

    def test_refusal_reason_is_bounded(self) -> None:
        """The journal truncates at 200 chars; the golden respects its own rule."""
        reason = GOLDEN_PAYLOADS[EventType.EXTENSION_REFUSED]["reason"]
        assert isinstance(reason, str)
        assert len(reason) <= 200


class TestIdempotencyDiscriminator:
    """The `install/refuse/activate` discriminators fit the ADR-0008 key."""

    def test_key_shape_matches_adr0008(self) -> None:
        cid = new_correlation_id()
        etype = EventType.EXTENSION_INSTALLED
        event = _event(etype, cid, 1, **GOLDEN_PAYLOADS[etype])
        event = event.model_copy(update={"discriminator": _DISCRIMINATORS[etype]})
        assert event.idempotency_key == compute_idempotency_key(
            cid, etype, attempt=0, discriminator=_DISCRIMINATORS[etype]
        )

    def test_install_and_activation_are_distinct_rows(self) -> None:
        """One install action journals two events; the discriminators differ."""
        cid = new_correlation_id()
        keys = {
            compute_idempotency_key(
                cid,
                etype,
                attempt=0,
                discriminator=_DISCRIMINATORS[etype],
            )
            for etype in (EventType.EXTENSION_INSTALLED, EventType.EXTENSION_ACTIVATED)
        }
        assert len(keys) == 2

    async def test_redelivery_dedupes_but_attempt_does_not(self, ledger: LedgerStore) -> None:
        """Same discriminator suppresses; a bumped attempt is a new event."""
        cid = new_correlation_id()
        etype = EventType.EXTENSION_INSTALLED
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
    """Extension events move no phase, close nothing, and are never unknown."""

    def test_all_three_are_in_no_phase_change(self) -> None:
        for etype in EXTENSION_TYPES:
            assert etype in _NO_PHASE_CHANGE, f"{etype.value} must be informational"
            assert etype not in _PHASE_MAP, f"{etype.value} must not move a phase"

    def test_none_is_terminal(self) -> None:
        for etype in EXTENSION_TYPES:
            assert etype not in TERMINAL_EVENTS, f"{etype.value} must never close a lineage"

    def test_extension_events_interleaved_in_a_task_do_not_perturb_it(self) -> None:
        cid = new_correlation_id()
        task = [
            _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
            _event(EventType.EXECUTION_STARTED, cid, 2, step_index=0),
            _event(EventType.EXECUTION_COMPLETED, cid, 3, step_index=0, tokens_consumed=10),
            _event(EventType.MUTATION_COMMITTED, cid, 4, applied_patch_sha256="a" * 64),
        ]
        interleaved = [
            _gold(EventType.EXTENSION_INSTALLED, cid, 5),
            _gold(EventType.EXTENSION_ACTIVATED, cid, 6),
        ]
        full = replay(task)
        mixed = replay(task[:2] + interleaved + task[2:])
        assert mixed.phase is full.phase
        assert mixed.unknown_events == []
        assert mixed.completed_steps == full.completed_steps
        assert mixed.tokens_consumed == full.tokens_consumed

    @given(seq=st.lists(st.sampled_from(list(EXTENSION_TYPES)), min_size=1, max_size=10))
    @hyp_settings(max_examples=60, deadline=None)
    def test_replay_total_over_extension_orderings(self, seq: list[EventType]) -> None:
        """Replay is total over every ordering of the three types: no
        ordering records an unknown event, moves a phase, or reads an
        extension-only lineage as task work with attempts."""
        cid = uuid.uuid4()
        events = [_event(et, cid, i + 1, **GOLDEN_PAYLOADS[et]) for i, et in enumerate(seq)]
        state = replay(events)
        assert state.unknown_events == []
        assert state.attempts == 0
        assert not state.is_terminal


class TestTierFoldIgnoresExtensionEvents:
    def test_apply_tier_event_is_a_noop_for_all_three(self) -> None:
        cid = new_correlation_id()
        state: dict[str, str] = {}
        for i, etype in enumerate(EXTENSION_TYPES):
            folded = apply_tier_event(
                state, _event(etype, cid, i + 1, **GOLDEN_PAYLOADS[etype])
            )
            assert folded == state == {}


class TestStoreRoundTrip:
    async def test_an_install_lineage_round_trips_and_chains(
        self, ledger: LedgerStore
    ) -> None:
        """The shape one real install writes: installed then activated,
        both on the install action's lineage, in ledger order."""
        cid = new_correlation_id()
        installed = dict(GOLDEN_PAYLOADS[EventType.EXTENSION_INSTALLED])
        activated = dict(GOLDEN_PAYLOADS[EventType.EXTENSION_ACTIVATED])
        # The payload's `lineage` restates the correlation id it rides.
        installed["lineage"] = activated["lineage"] = str(cid)
        for etype, payload in (
            (EventType.EXTENSION_INSTALLED, installed),
            (EventType.EXTENSION_ACTIVATED, activated),
        ):
            await ledger.append(
                LedgerEvent.create(
                    cid, etype, payload=payload, discriminator=_DISCRIMINATORS[etype]
                )
            )
        rows = await ledger.read_correlation(cid)
        assert [row.event_type for row in rows] == [
            EventType.EXTENSION_INSTALLED,
            EventType.EXTENSION_ACTIVATED,
        ]
        assert [row.payload for row in rows] == [installed, activated]
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems
        head = await ledger.head(cid)
        assert head is not None
        assert not head.is_terminal  # the head must not close an install lineage


class TestRecoverySweepSkipsExtensionLineages:
    """An install is an operator action, never resumable task work."""

    async def test_extension_only_lineage_is_skipped_never_requeued(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        from tests.conftest import RecordingRequeuer

        cid = new_correlation_id()
        installed = dict(GOLDEN_PAYLOADS[EventType.EXTENSION_INSTALLED])
        activated = dict(GOLDEN_PAYLOADS[EventType.EXTENSION_ACTIVATED])
        installed["lineage"] = activated["lineage"] = str(cid)
        for etype, payload in (
            (EventType.EXTENSION_INSTALLED, installed),
            (EventType.EXTENSION_ACTIVATED, activated),
        ):
            await ledger.append(
                LedgerEvent.create(
                    cid, etype, payload=payload, discriminator=_DISCRIMINATORS[etype]
                )
            )

        requeuer = RecordingRequeuer()
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.scanned == 1
        assert report.count(RecoveryOutcome.SKIPPED_EXTENSION_LINEAGE) == 1
        assert requeuer.calls == []  # an install is never re-queued as a task
        # The sweep wrote nothing: the gates are atomic, there is no
        # half-install to reconcile, so the lineage is exactly what it was.
        kinds = [e.event_type for e in await ledger.read_correlation(cid)]
        assert kinds == [EventType.EXTENSION_INSTALLED, EventType.EXTENSION_ACTIVATED]

    async def test_extension_events_do_not_flatten_a_task_lineage(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        """A lineage mixing TASK_* rows with EXTENSION rows is a task
        lineage the ordinary path recovers; the phase-inert EXTENSION
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
                EventType.EXTENSION_ACTIVATED,
                payload=dict(GOLDEN_PAYLOADS[EventType.EXTENSION_ACTIVATED]),
                discriminator=_DISCRIMINATORS[EventType.EXTENSION_ACTIVATED],
            )
        )

        requeuer = RecordingRequeuer()
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_EXTENSION_LINEAGE) == 0
        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
