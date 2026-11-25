"""The thread journal — the four thread event types' emitter (C7.5).

The ADR-0046 pattern the coder journal, the chat journal and the pipeline
journal each instantiated, instantiated once more for the control plane:
one class, one lineage, one v1 payload per event type, every payload
bounded by construction. The thread id **is** the correlation id, so
``paa ledger <thread-id>`` is the thread's whole history with no mapping.

Payload discipline, stated where it is enforced:

* every payload carries ``v: 1`` — a version bump is a change of record
  and updates the golden tests in the same commit;
* operator free text never enters a payload. The thread **title** is
  hashed, counted and shape-previewed (``<str len=N sha256=xxxxxxxx>``,
  the coder/chat preview rule) — a title is exactly the kind of opaque
  input a credential can hide in, and the ledger must never carry one;
* the workspace path is scrubbed through :func:`paa.models.base.redact`,
  the same treatment the mandate's own scope payload gives it;
* harness names and segment ids are identifiers from closed vocabularies
  (the binding registry; uuid minting), not prose — they ride verbatim.

Idempotency is the full ADR-0008 key with ``attempt=0``: the
discriminators are ``created``, ``segment:<id>:STARTED``,
``segment:<id>:ENDED`` and ``swap:<segment>:<to>``, so a redelivered
append dedupes in the store while one segment's STARTED and ENDED stay
distinct rows — the chat journal's rule, unchanged.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Coroutine
from typing import Any

from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.models.base import redact

__all__ = ["THREAD_EVENT_TYPES", "ThreadJournal"]

#: The thread control plane's own vocabulary — the four members C7.5 adds
#: to ``EventType``. All are informational (``_NO_PHASE_CHANGE`` in
#: :mod:`paa.ledger.replay`, never in ``TERMINAL_EVENTS``): a thread is
#: not a task lineage, its events move no phase and close nothing, and a
#: thread-only lineage is exempt from the boot sweep the way a chat-only
#: one is (:mod:`paa.ledger.recovery`).
THREAD_EVENT_TYPES = (
    EventType.THREAD_CREATED,
    EventType.THREAD_SEGMENT_STARTED,
    EventType.THREAD_SEGMENT_ENDED,
    EventType.THREAD_HARNESS_SWAPPED,
)

#: The reasons a segment can end, restated from the record module's own
#: frozen vocabulary. Restated rather than imported because it is two
#: words and a cross-package constant import would buy nothing the golden
#: tests do not already pin: both spellings fail loudly if either side
#: drifts.
_REASONS = frozenset({"completed", "interrupted"})


def title_sha256(title: str) -> str:
    """SHA-256 of the title's UTF-8 bytes — the ledger's attestation."""
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


def title_preview(title: str) -> str:
    """A shape summary of the title, never its content.

    The same rendering the coder and chat journals use (``<str len=N
    sha256=xxxxxxxx>``): length plus a short digest fingerprint, so two
    different titles read as different while neither is disclosed.
    """
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    return f"<str len={len(title)} sha256={digest}>"


class ThreadJournal:
    """Append the thread vocabulary to one thread's lineage.

    Constructed with the runtime's :class:`~paa.ledger.store.LedgerStore`
    and the thread id (which is the lineage key). Adds no dedupe of its
    own — the store's append transaction is the one door, per the house
    rule that nothing appends outside :meth:`LedgerStore.append`.
    """

    def __init__(self, ledger: LedgerStore, thread_id: uuid.UUID) -> None:
        self._ledger = ledger
        self._thread_id = thread_id

    @property
    def thread_id(self) -> uuid.UUID:
        return self._thread_id

    def _append(
        self, event_type: EventType, payload: dict[str, Any], discriminator: str
    ) -> Coroutine[Any, Any, LedgerEvent]:
        return self._ledger.append(
            LedgerEvent.create(
                self._thread_id,
                event_type,
                session_id=None,
                attempt=0,
                discriminator=discriminator,
                payload=payload,
            )
        )

    # -- lifecycle ----------------------------------------------------------

    async def created(
        self, *, title: str, workspace: str, status: str
    ) -> LedgerEvent:
        """Append ``THREAD_CREATED`` — the thread's birth record.

        Emitted after the mandate grant and before the thread file is
        first written: the ledger's row is the fact, the file is the
        projection of it.
        """
        payload: dict[str, Any] = {
            "v": 1,
            "thread_id": str(self._thread_id),
            "title_sha256": title_sha256(title),
            "title_chars": len(title),
            "title_preview": title_preview(title),
            "workspace": redact(workspace),
            "status": status,
        }
        return await self._append(EventType.THREAD_CREATED, payload, "created")

    async def segment_started(
        self,
        segment_id: str,
        *,
        harness: str,
        packet_sha256: str,
        workspace: str,
    ) -> LedgerEvent:
        """Append ``THREAD_SEGMENT_STARTED`` — the ledger-before-effect row.

        Emitted **before** the harness spawns: a crash between this row and
        the child's exit leaves an open segment the engine closes as
        ``interrupted`` on the next touch, which is only recoverable
        because the row came first.
        """
        payload: dict[str, Any] = {
            "v": 1,
            "thread_id": str(self._thread_id),
            "segment_id": segment_id,
            "harness": harness,
            "packet_sha256": packet_sha256,
            "workspace": redact(workspace),
        }
        return await self._append(
            EventType.THREAD_SEGMENT_STARTED,
            payload,
            f"segment:{segment_id}:STARTED",
        )

    async def segment_ended(
        self,
        segment_id: str,
        *,
        harness: str,
        reason: str,
        exit_code: int | None,
        timed_out: bool,
        duration_ms: float,
        record_run_dir: str | None,
    ) -> LedgerEvent:
        """Append ``THREAD_SEGMENT_ENDED`` — the stint's outcome row.

        ``exit_code`` is the harness's own fate (``None`` when the stint
        was interrupted before an exit existed); ``record_run_dir`` points
        at the recorder's run directory when the stint ran under
        ``--record``, and is ``None`` for a plainly contained stint — the
        two containment stories are named, never collapsed.
        """
        if reason not in _REASONS:
            raise ValueError(
                f"segment end reason must be one of {sorted(_REASONS)}, got {reason!r}"
            )
        payload: dict[str, Any] = {
            "v": 1,
            "thread_id": str(self._thread_id),
            "segment_id": segment_id,
            "harness": harness,
            "reason": reason,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": round(duration_ms, 3),
            "record_run_dir": record_run_dir,
        }
        return await self._append(
            EventType.THREAD_SEGMENT_ENDED,
            payload,
            f"segment:{segment_id}:ENDED",
        )

    async def harness_swapped(
        self, *, from_harness: str, to_harness: str, after_segment: str
    ) -> LedgerEvent:
        """Append ``THREAD_HARNESS_SWAPPED`` — the transition row.

        The swap's own record, distinct from the segments it sits between:
        the previous segment's ENDED row says the stint stopped, this row
        says the *thread* chose a different hand, and the next STARTED row
        opens the new stint. Three rows, three facts — collapsing them
        into one would lose the question ``paa thread show`` answers with
        them: what ran, when it stopped, and what took over.
        """
        payload: dict[str, Any] = {
            "v": 1,
            "thread_id": str(self._thread_id),
            "from_harness": from_harness,
            "to_harness": to_harness,
            "after_segment": after_segment,
        }
        return await self._append(
            EventType.THREAD_HARNESS_SWAPPED,
            payload,
            f"swap:{after_segment}:{to_harness}",
        )
