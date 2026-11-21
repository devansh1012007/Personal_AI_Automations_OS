"""Chat sessions: the record, the ledger journal, and the fold that rebuilds it.

The ledger is the truth, so a chat session has no table of its own. One
session IS one correlation lineage — the session id *is* the lineage key —
and everything the product needs to know about a session (when it started,
who mandated it, how many turns it served, whether it ended) is folded from
that lineage's events by :func:`fold_session`. "List sessions" is a scan of
``CHAT_SESSION_STARTED`` events through :meth:`LedgerStore.events_since`,
never a private index: a second place for session state would be a second
truth, and the first time the two disagreed, the ledger would lose.

The journal (:class:`ChatSessionJournal`) is the ADR-0046 pattern applied to
conversation. Every payload is ``v=1``; every free-text field is bounded;
message text never enters a payload — only its SHA-256, its length, and a
shape-only preview, exactly the discipline the coder journal states for tool
arguments (``coder/journal.py``): a chat message is an *opaque* input, and no
secret pattern can be trusted to recognise every credential an opaque input
may carry. The verbatim words live in the cold lake (``cold_lake_signals``,
channel ``chat.turn``), which is the substrate designed to hold raw input.

Idempotency is the full ADR-0008 key: one session lineage, ``attempt`` 0,
and a discriminator that names the turn and the outcome kind
(``<turn_id>:PROPOSED`` and friends) — so a redelivered append dedupes in
the store, while turn 1's ``PROPOSED`` and turn 2's are distinct events.
No payload carries the exact key ``fact_id`` (plural ``fact_ids`` only):
``apply_tier_event`` folds any event naming a scalar ``fact_id`` into tier
state, and a chat payload has no business moving a fact's retention tier.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from paa.core.errors import PaaError
from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore

__all__ = [
    "STATUS_ACTIVE",
    "STATUS_ENDED",
    "ChatSessionJournal",
    "ChatSessionRecord",
    "ChatTurnStatus",
    "fold_session",
    "list_sessions",
    "session_record",
    "text_preview",
    "text_sha256",
    "turn_discriminator",
]

#: The cold-lake channel every chat signal files under. One channel, one
#: reader: episodic recall filters this channel by the session id carried in
#: each signal's payload (and its ``external_id``, which names
#: ``<session_id>:<turn_id>:<role>``).
CHAT_CHANNEL: Final = "chat.turn"

STATUS_ACTIVE: Final = "active"
STATUS_ENDED: Final = "ended"

#: Outcome kinds, uppercase to match the coder journal's discriminator
#: vocabulary. One turn's outcomes are distinct events; a redelivered
#: outcome dedupes against its own kind.
_KIND_PROPOSED: Final = "PROPOSED"
_KIND_COMPLETED: Final = "COMPLETED"
_KIND_FAILED: Final = "FAILED"
_KIND_CANCELLED: Final = "CANCELLED"

_OUTCOME_KINDS: Final[frozenset[str]] = frozenset(
    {_KIND_COMPLETED, _KIND_FAILED, _KIND_CANCELLED}
)

#: Free-text budget. The ledger stays readable and the payload shape stays
#: bounded no matter what a caller was handed; a refusal reason longer than
#: this carries no extra information, only risk.
_MAX_REASON_CHARS: Final = 200

#: List budget for ``fact_ids`` / ``signal_ids`` payloads. ``recall_k`` is
#: small, but the payload bound must not depend on a caller honouring it.
_MAX_ID_LIST: Final = 100

#: The event vocabulary this journal owns. The recovery sweep's
#: chat-only-lineage test (``ledger/recovery.py``) reads this same set from
#: ``core.types``; duplicating it here would let the two drift apart.
CHAT_EVENT_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.CHAT_SESSION_STARTED,
        EventType.CHAT_SESSION_ENDED,
        EventType.CHAT_TURN_PROPOSED,
        EventType.CHAT_TURN_COMPLETED,
        EventType.CHAT_TURN_FAILED,
        EventType.CHAT_TURN_CANCELLED,
        EventType.MEMORY_RECALL_CONSULTED,
        EventType.MEMORY_SIGNAL_EXTRACTED,
    }
)

#: The per-turn discriminator kinds, for tests and the sweep.
TURN_DISCRIMINATOR_KINDS: Final[tuple[str, ...]] = (
    _KIND_PROPOSED,
    _KIND_COMPLETED,
    _KIND_FAILED,
    _KIND_CANCELLED,
)


class ChatTurnStatus:
    """The three terminal states of one turn, as payload/status strings.

    A module-level namespace rather than an enum only because these are
    also the discriminator kinds and the recovery sweep's vocabulary; one
    source of constants, three names.
    """

    COMPLETED = _KIND_COMPLETED
    FAILED = _KIND_FAILED
    CANCELLED = _KIND_CANCELLED


def turn_discriminator(turn_id: str, kind: str) -> str:
    """The ADR-0008 discriminator for one turn outcome.

    ``turn_id`` is a uuid; a ``:`` in it would make the split ambiguous the
    way the coder journal's ``session:call_id:kind`` guard describes, so a
    malformed id is refused before it can dedupe two different turns into
    one key.
    """
    if not turn_id or ":" in turn_id:
        raise ValueError(
            f"turn id must be non-empty and free of ':', got {turn_id!r}"
        )
    if kind not in _OUTCOME_KINDS and kind != _KIND_PROPOSED:
        raise ValueError(f"unknown turn discriminator kind {kind!r}")
    return f"{turn_id}:{kind}"


def text_sha256(text: str) -> str:
    """SHA-256 of the UTF-8 bytes of one message. The ledger's attestation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_preview(text: str) -> str:
    """A shape summary of one message, never its content.

    Mirrors the coder journal's string rendering (``<str len=N
    sha256=xxxxxxxx>``): length plus a short digest fingerprint, so two
    different messages read as different while neither is disclosed. This
    is the only preview rule that holds against an opaque secret — a
    credential with no recognisable shape, handed to no one as a literal —
    and chat messages are exactly where such credentials show up ("my API
    key is ...").
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"<str len={len(text)} sha256={digest}>"


def _bound_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    return reason[:_MAX_REASON_CHARS]


def _bound_ids(ids: Sequence[str]) -> list[str]:
    return [str(i) for i in ids][:_MAX_ID_LIST]


def _safe_int(value: Any) -> int:
    """An int from a payload, or 0. The fold is total; ``True`` is not 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _safe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class ChatSessionRecord:
    """One chat session, folded from its ledger lineage.

    Every field is derived; nothing here is written anywhere. ``status``
    follows the *last* session-scoped marker: an ``ENDED`` after which more
    turns arrive reads ``active`` again, because that is what the lineage
    says happened — the session has activity, whatever a previous close
    claimed. The record is a view, not a gate.
    """

    session_id: uuid.UUID
    created_at: datetime | None
    scope: str
    mandate_ref: str | None
    status: str
    stopped_reason: str | None
    model: str | None
    cross_session_episodic: bool
    turns_proposed: int
    turns_completed: int
    turns_failed: int
    turns_cancelled: int
    tokens_consumed: int
    state_version: int


def fold_session(events: Iterable[LedgerEvent]) -> ChatSessionRecord | None:
    """Fold one session lineage's events into a :class:`ChatSessionRecord`.

    Total over any input: an empty lineage, a lineage without a
    ``CHAT_SESSION_STARTED`` (a crash before the start row landed), or a
    lineage carrying unknown event types all fold without raising. ``None``
    means "no session here at all" — the caller distinguishes an empty
    lineage from a session it should render.

    The fold reads payloads defensively (type-checked, never ``int(...)``)
    for the same reason ``apply_event`` does: recovery folds histories
    written by older code, and one malformed row must not cost the well-
    formed rows beside it.
    """
    session_id: uuid.UUID | None = None
    created_at: datetime | None = None
    scope = "personal"
    mandate_ref: str | None = None
    model: str | None = None
    cross_session_episodic = False
    status = STATUS_ACTIVE
    stopped_reason: str | None = None
    proposed = completed = failed = cancelled = 0
    tokens = 0
    version = 0

    for event in events:
        version = event.state_version or version
        if session_id is None:
            session_id = event.correlation_id
        if created_at is None:
            created_at = event.recorded_at
        payload = event.payload
        etype = event.event_type

        if etype is EventType.CHAT_SESSION_STARTED:
            created_at = event.recorded_at
            scope = _safe_str(payload.get("scope")) or scope
            mandate_ref = _safe_str(payload.get("mandate_ref"))
            model = _safe_str(payload.get("model"))
            cross_session_episodic = payload.get("cross_session_episodic") is True
            # A (re-)start is activity, the same way a proposal is: the only
            # STARTED a fold can see after an ENDED is a lineage re-opened on
            # purpose, and the record is a view of what the lineage says.
            status = STATUS_ACTIVE
            stopped_reason = None
        elif etype is EventType.CHAT_SESSION_ENDED:
            status = STATUS_ENDED
            stopped_reason = _safe_str(payload.get("stopped_reason"))
        elif etype is EventType.CHAT_TURN_PROPOSED:
            status = STATUS_ACTIVE
            stopped_reason = None
            proposed += 1
        elif etype is EventType.CHAT_TURN_COMPLETED:
            completed += 1
            tokens += _safe_int(payload.get("tokens_consumed"))
        elif etype is EventType.CHAT_TURN_FAILED:
            failed += 1
        elif etype is EventType.CHAT_TURN_CANCELLED:
            cancelled += 1

    if session_id is None:
        return None
    return ChatSessionRecord(
        session_id=session_id,
        created_at=created_at,
        scope=scope,
        mandate_ref=mandate_ref,
        status=status,
        stopped_reason=stopped_reason,
        model=model,
        cross_session_episodic=cross_session_episodic,
        turns_proposed=proposed,
        turns_completed=completed,
        turns_failed=failed,
        turns_cancelled=cancelled,
        tokens_consumed=tokens,
        state_version=version,
    )


class ChatSessionJournal:
    """Append the chat event vocabulary to one session's lineage.

    Constructed with the runtime's :class:`~paa.ledger.store.LedgerStore`
    and the session id (which is the lineage key). ``attempt`` is always 0
    and the discriminator names the turn and the kind, so a redelivered
    append returns the existing event through the store's own
    dedupe-then-seal transaction; the journal adds no second dedupe.

    Payloads are built from explicit field lists and are structurally
    bounded (ids are uuids, reasons and id-lists are capped), so the 8 KB
    inline payload limit cannot be breached by construction — there is no
    blob plumbing here because there is nothing to blob.
    """

    def __init__(self, ledger: LedgerStore, session_id: uuid.UUID) -> None:
        self._ledger = ledger
        self._session_id = session_id

    @property
    def session_id(self) -> uuid.UUID:
        return self._session_id

    def _append(
        self, event_type: EventType, payload: dict[str, Any], discriminator: str
    ) -> Awaitable[LedgerEvent]:
        return self._ledger.append(
            LedgerEvent.create(
                self._session_id,
                event_type,
                session_id=self._session_id,
                attempt=0,
                discriminator=discriminator,
                payload=payload,
            )
        )

    # -- session lifecycle --------------------------------------------------

    async def session_started(
        self,
        *,
        scope: str,
        mandate_ref: str | None,
        model: str,
        cross_session_episodic: bool,
    ) -> LedgerEvent:
        """Append CHAT_SESSION_STARTED. Idempotent per lineage."""
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "scope": scope,
            "mandate_ref": mandate_ref,
            "model": model,
            "cross_session_episodic": cross_session_episodic,
        }
        return await self._append(EventType.CHAT_SESSION_STARTED, payload, "session-started")

    async def session_ended(
        self,
        *,
        stopped_reason: str,
        turns_proposed: int,
        turns_completed: int,
        turns_failed: int,
        turns_cancelled: int,
        tokens_consumed: int,
    ) -> LedgerEvent:
        """Append CHAT_SESSION_ENDED, carrying the fold's own counters."""
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "stopped_reason": _bound_reason(stopped_reason) or "user",
            "turns_proposed": turns_proposed,
            "turns_completed": turns_completed,
            "turns_failed": turns_failed,
            "turns_cancelled": turns_cancelled,
            "tokens_consumed": tokens_consumed,
        }
        return await self._append(EventType.CHAT_SESSION_ENDED, payload, "session-ended")

    # -- turns ---------------------------------------------------------------

    async def turn_proposed(self, turn_id: str, text: str) -> LedgerEvent:
        """Append CHAT_TURN_PROPOSED — the ledger-before-side-effect row.

        Called before the turn's first effect (recall, model call, signal
        write). The text itself is attested, not carried: hash, length,
        shape preview.
        """
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "turn_id": turn_id,
            "text_sha256": text_sha256(text),
            "text_chars": len(text),
            "text_preview_redacted": text_preview(text),
        }
        return await self._append(
            EventType.CHAT_TURN_PROPOSED,
            payload,
            turn_discriminator(turn_id, _KIND_PROPOSED),
        )

    async def turn_completed(
        self,
        turn_id: str,
        *,
        reply: str,
        tokens_consumed: int,
        recalled_fact_ids: Sequence[str],
        signal_ids: Sequence[str],
        facts_written: Sequence[str],
    ) -> LedgerEvent:
        """Append CHAT_TURN_COMPLETED — proof the turn produced a reply."""
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "turn_id": turn_id,
            "reply_sha256": text_sha256(reply),
            "reply_chars": len(reply),
            "tokens_consumed": tokens_consumed,
            "recalled_fact_ids": _bound_ids(recalled_fact_ids),
            "signal_ids": _bound_ids(signal_ids),
            "facts_written": _bound_ids(facts_written),
        }
        return await self._append(
            EventType.CHAT_TURN_COMPLETED,
            payload,
            turn_discriminator(turn_id, _KIND_COMPLETED),
        )

    async def turn_failed(self, turn_id: str, *, reason: str | None) -> LedgerEvent:
        """Append CHAT_TURN_FAILED — the turn ended in an error, not a cancel.

        The reason is bounded and ledger-safe (callers pass a redacted
        classification, never a raw exception with data in it).
        """
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "turn_id": turn_id,
            "reason": _bound_reason(reason) or "turn failed",
        }
        return await self._append(
            EventType.CHAT_TURN_FAILED,
            payload,
            turn_discriminator(turn_id, _KIND_FAILED),
        )

    async def turn_cancelled(
        self, turn_id: str, *, partial_reply: str | None
    ) -> LedgerEvent:
        """Append CHAT_TURN_CANCELLED — the operator stopped the turn.

        A cancelled turn is not a failed turn: the vocabulary separates
        them so the TUI (and the record) can say "stopped" without
        implying anything broke. The partial reply — the deltas that
        already streamed — is attested by hash and length, never carried.
        """
        partial = partial_reply or ""
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "turn_id": turn_id,
            "partial_reply_sha256": text_sha256(partial) if partial else None,
            "partial_reply_chars": len(partial),
        }
        return await self._append(
            EventType.CHAT_TURN_CANCELLED,
            payload,
            turn_discriminator(turn_id, _KIND_CANCELLED),
        )

    # -- memory consults ------------------------------------------------------

    async def recall_consulted(
        self,
        *,
        turn_id: str | None,
        query: str,
        k: int,
        fact_ids: Sequence[str],
        refused: bool = False,
        refusal_reason: str | None = None,
    ) -> LedgerEvent:
        """Append MEMORY_RECALL_CONSULTED — what memory served, and what it refused.

        ``refused=True`` is the cross-session episodic fence's ledger row:
        a policy denial recorded where the denial happened, named by
        ``refusal_reason`` (the plan's "refusals are ledger events", kept
        on this informational event rather than ``POLICY_BLOCKED``,
        which is a terminal *task* verdict and would close the session's
        lineage).
        """
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "turn_id": turn_id,
            "query_sha256": text_sha256(query),
            "query_chars": len(query),
            "k": k,
            "returned": len(fact_ids),
            "fact_ids": _bound_ids(fact_ids),
            "refused": refused,
            "refusal_reason": _bound_reason(refusal_reason),
        }
        discriminator = f"recall:{turn_id or 'session'}:{text_sha256(query)[:12]}"
        return await self._append(EventType.MEMORY_RECALL_CONSULTED, payload, discriminator)

    async def signal_extracted(
        self,
        *,
        turn_id: str | None,
        signal_id: str,
        outcome: str,
        facts_written: Sequence[str],
    ) -> LedgerEvent:
        """Append MEMORY_SIGNAL_EXTRACTED — what the MemoryCreator made of a turn signal."""
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": str(self._session_id),
            "turn_id": turn_id,
            "signal_id": signal_id,
            "outcome": outcome,
            "facts_written": _bound_ids(facts_written),
        }
        return await self._append(
            EventType.MEMORY_SIGNAL_EXTRACTED,
            payload,
            f"signal:{signal_id}",
        )


async def session_record(
    store: LedgerStore, session_id: uuid.UUID
) -> ChatSessionRecord | None:
    """Fold one session's lineage into its record. ``None`` if no lineage."""
    events = await store.read_correlation(session_id)
    return fold_session(events)


async def list_sessions(
    store: LedgerStore, *, limit: int = 100
) -> list[ChatSessionRecord]:
    """Every session with a ``CHAT_SESSION_STARTED``, newest first.

    Built by replay, not a private index (the module docstring says why
    this matters). The scan reads the ledger's ``CHAT_SESSION_STARTED``
    rows through :meth:`LedgerStore.events_since` — newest first, so the
    ``limit`` cut evicts the oldest sessions, never the newest — then folds
    each session's full lineage. The order is total: ``recorded_at DESC``,
    then the session id as tiebreak, so two sessions started in the same
    microsecond still list deterministically.
    """
    since = datetime(1970, 1, 1, tzinfo=UTC)
    starts = await store.events_since(
        since,
        event_types=[EventType.CHAT_SESSION_STARTED],
        limit=limit,
        descending=True,
    )
    records: list[ChatSessionRecord] = []
    for start in starts:
        record = await session_record(store, start.correlation_id)
        if record is not None:
            records.append(record)
    records.sort(
        key=lambda r: (
            _sort_key(r.created_at),
            str(r.session_id),
        ),
        reverse=True,
    )
    return records[: max(0, limit)]


def _sort_key(value: datetime | None) -> str:
    """ISO text of a timestamp, or the empty epoch. Total over ``None``."""
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat()


class SessionNotFoundError(PaaError):
    """A session id that names no ``CHAT_SESSION_STARTED`` lineage."""
