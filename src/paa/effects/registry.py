"""The effect registry: a reader over the ledger, never a second store.

The one invariant — *the ledger is the truth* — decides this module's
shape. Effect rows ARE the ledger's rows: a versioned ``effect`` field on
``PIPELINE_CALL_COMPLETED`` payloads (ADR-0054), written by the call
journal's :meth:`~paa.pipelines.journal.CallJournal.call_completed` door.
The registry holds no state of its own; every method reads the ledger and
derives, so a rebuilt process reconstructs the identical view and there is
no snapshot, no cache, and no recovery rule to write.

Two derivations:

* :meth:`EffectRegistry.effects_for_scope` — the effect-ownership graph's
  rows for one scope, **newest-first** (rollback unwinds in reverse
  application order, the VISION appendix's LIFO disposal rule);
* :meth:`EffectRegistry.compensated_effect_ids` — the set of effect ids
  whose compensation is *recorded as completed* anywhere in the ledger,
  derived by scanning ``PIPELINE_CALL_COMPLETED`` rows for the
  ``compensates`` field the rollback service writes. This is the
  idempotency set: it is recomputed from the ledger on every run, never
  held in process state, which is what makes a crash-resume free of
  double compensation.

Both readings resolve blobbed rows through the blob store when one is
supplied (a call result past the 8 KB inline limit blobs the whole
payload, effect field included — the N2.6 plumbing, shared with the call
journal's own fold). A blobbed row that cannot be resolved **refuses**
rather than degrades: an effect that reads as absent is an effect a
rollback would re-compensate, and a compensation that reads as absent is
one it would double-compensate — both are the silence this module exists
to not have.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from paa.core.errors import PaaError
from paa.core.types import EventType
from paa.effects.spec import EffectSpec
from paa.ledger.events import (
    BlobStoreLike,
    LedgerEvent,
    is_blobbed_payload,
    resolve_blobbed_payload,
)
from paa.ledger.store import LedgerStore

__all__ = [
    "EffectRegistry",
    "EffectRegistryError",
    "ScopeEffect",
]

#: The scan ceiling for the compensated-set derivation. The store's own
#: time-ranged scans default to 10,000 rows; the registry uses the same
#: number and *refuses on saturation* rather than silently reading a
#: prefix — a truncated compensated set is the double-compensation bug
#: wearing a success exit code.
_SCAN_LIMIT: Final = 10_000

#: The oldest timestamp the compensated-set scan can name. ``recorded_at``
#: is always a real wall clock (2026-era), so the unix epoch is before
#: every row there will ever be.
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: The one event type whose payloads can carry the ``effect`` field in v1
#: (ADR-0054). Kept as a module constant so the registry and the journal
#: door cannot drift on which vocabulary owns effects.
EFFECT_BEARING_EVENT_TYPES: Final = (EventType.PIPELINE_CALL_COMPLETED,)


class EffectRegistryError(PaaError):
    """The registry cannot derive a truthful view from the ledger."""


@dataclass(frozen=True, slots=True)
class ScopeEffect:
    """One effect of a scope, with the ledger row that carries it.

    ``spec`` is the parsed :class:`~paa.effects.spec.EffectSpec`;
    ``event_id`` and ``state_version`` come from the row's envelope, so a
    report can name the exact row an effect rode and a reader can verify
    it against the hash chain.
    """

    spec: EffectSpec
    event_id: str
    state_version: int | None


class EffectRegistry:
    """The derived index over effect-bearing ledger rows.

    Constructed with the store (and, when the deployment has one, the blob
    store); holds nothing else. Every call re-reads, which is the point:
    the registry is a *query*, not a materialized view, and the ledger
    underneath it is append-only.
    """

    def __init__(
        self,
        store: LedgerStore,
        *,
        blob_store: BlobStoreLike | None = None,
    ) -> None:
        self._store = store
        self._blob_store = blob_store

    def _resolve_payload(self, event: LedgerEvent) -> dict[str, Any]:
        """One row's payload, blob-resolved; a stub without a store refuses.

        The refusal is deliberate and named: for effect rows it prevents a
        hidden effect reading as absent, and for compensation rows it
        prevents a recorded compensation reading as absent. Both failures
        are the same bug — the registry quietly understating what the
        ledger holds — and both are refused rather than guessed at.
        """
        if not is_blobbed_payload(dict(event.payload)):
            return dict(event.payload)
        if self._blob_store is None:
            raise EffectRegistryError(
                "a blobbed call row cannot be resolved without a blob store",
                event_type=event.event_type.value,
                lineage=str(event.correlation_id),
                discriminator=event.discriminator,
            )
        try:
            return resolve_blobbed_payload(dict(event.payload), self._blob_store)
        except (ValueError, PaaError) as exc:
            raise EffectRegistryError(
                f"a blobbed call row failed to resolve: {exc}",
                event_type=event.event_type.value,
                lineage=str(event.correlation_id),
                discriminator=event.discriminator,
            ) from exc

    async def effects_for_scope(self, scope: uuid.UUID) -> list[ScopeEffect]:
        """The scope's effects, newest-first (highest owner seq first).

        Rows of the scope's own lineage are read in replay order and
        returned in reverse, so a rollback walks them in the opposite
        order the effects were applied — the LIFO disposal rule the VISION
        appendix states for owned effects. Malformed ``effect`` fields
        read as no effect (the spec's total-reader rule); a blobbed row
        that cannot be resolved refuses the whole derivation.
        """
        events = await self._store.read_correlation(scope)
        found: list[ScopeEffect] = []
        for event in events:
            if event.event_type not in EFFECT_BEARING_EVENT_TYPES:
                continue
            payload = self._resolve_payload(event)
            spec = EffectSpec.from_payload(payload.get("effect"))
            if spec is None:
                continue
            found.append(
                ScopeEffect(
                    spec=spec,
                    event_id=str(event.event_id),
                    state_version=event.state_version,
                )
            )
        found.sort(key=lambda item: item.spec.owner.seq, reverse=True)
        return found

    async def compensated_effect_ids(self) -> set[str]:
        """Every effect id whose completed compensation is in the ledger.

        Scans ``PIPELINE_CALL_COMPLETED`` rows across all lineages for the
        ``compensates`` field (the rollback service writes it on every
        compensation it records). The set is the idempotency oracle: an
        effect in it is skipped by any later rollback, whatever process,
        whatever reboot — because the fact lives in the ledger, not in
        memory.
        """
        rows = await self._store.events_since(
            _EPOCH, event_types=EFFECT_BEARING_EVENT_TYPES, limit=_SCAN_LIMIT
        )
        if len(rows) >= _SCAN_LIMIT:
            raise EffectRegistryError(
                "the compensated-set scan saturated its row limit; refusing to "
                "read a prefix of the ledger (a truncated set would double-"
                "compensate)",
                limit=_SCAN_LIMIT,
            )
        compensated: set[str] = set()
        for event in rows:
            if "compensates" not in event.payload and not is_blobbed_payload(
                dict(event.payload)
            ):
                # The fast path: an ordinary inline row without the field
                # cannot name a compensation. (A blobbed row might hide one
                # and is resolved below regardless.)
                continue
            payload = self._resolve_payload(event)
            ref = payload.get("compensates")
            if isinstance(ref, str) and ref:
                compensated.add(ref)
        return compensated
