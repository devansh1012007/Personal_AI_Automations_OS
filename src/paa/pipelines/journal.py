"""The generalised call journal: seq, fingerprint, outcome (C6.2 / P2.1).

The coder journal (``coder/journal.py``, N2.2) proved the pattern at
tool-call granularity: PROPOSED before the effect, the outcome after,
idempotent discriminators, payloads that carry hashes and shapes instead
of content. This module is the same discipline lifted one level — the
record of every *capability call* a pipeline makes through the facade,
which is what ADR-0050 names the checkpoint: after a crash the host
restarts the pipeline and answers already-completed calls from this
record without re-executing them, and a call sequence that no longer
matches the record stops the run (divergence, never resync).

Shape (ADR-0050 §Decision, the pseudocode made code):

* every call carries ``seq`` — monotonic within the run — and
  ``fingerprint = sha256(canonical_json({method, params}))``;
* ``call_proposed(seq, method, params)`` appends the PROPOSED row with
  the fingerprint and a shape-only preview of the params, never their
  content (a prompt can hold an opaque secret no pattern would catch —
  the coder journal's preview rule, restated for the new vocabulary);
* ``call_completed(seq, method, result)`` appends the outcome with the
  result's SHA-256 **and** its canonical JSON — the one deliberate
  content exception, because replay must serve the recorded value back
  and a digest cannot reconstruct it. Oversized results blob through
  the same N2.6 plumbing the coder journal uses;
* ``call_failed(seq, method, reason)`` closes a call that raised, and
  never attests a result.

Idempotency is the full ADR-0008 key: ``attempt=0`` and the
discriminator ``call:<seq>:<KIND>`` (run rows use ``run:<KIND>``), so a
redelivered append dedupes in the store's own transaction while one
call's PROPOSED and COMPLETED stay distinct rows.

The fold (:func:`fold_call_journal`) is total over any lineage — the
house replay rule — and is what recovery, replay and ``paa pipeline
trace`` read. It resolves blobbed results through the blob store when
one is supplied, so a replayed run serves the same value the original
call returned, from inline or from the lake alike.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from paa.core.types import EventType
from paa.ledger.events import (
    BlobStoreLike,
    LedgerEvent,
    canonical_json,
    is_blobbed_payload,
    maybe_blob_payload,
    payload_requires_blob,
    resolve_blobbed_payload,
)
from paa.ledger.store import LedgerStore

__all__ = [
    "PIPELINE_JOURNAL_EVENT_TYPES",
    "CallJournal",
    "CallOutcome",
    "CallRecord",
    "fingerprint",
    "fold_call_journal",
    "params_shape",
    "read_call_journal",
]


#: The event vocabulary this journal owns, for the recovery sweep's
#: pipeline-lineage determination and the conformance suite. One source
#: of constants; ``ledger/recovery.py`` keeps its own copy in sync with
#: ``core.types`` the way it does for chat — the sweep's set is the
#: authority for skipping, this one for journaling.
PIPELINE_JOURNAL_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.PIPELINE_RUN_STARTED,
        EventType.PIPELINE_RUN_COMPLETED,
        EventType.PIPELINE_RUN_FAILED,
        EventType.PIPELINE_RUN_DIVERGED,
        EventType.PIPELINE_CALL_PROPOSED,
        EventType.PIPELINE_CALL_COMPLETED,
        EventType.PIPELINE_CALL_FAILED,
    }
)

#: Free-text budget for reasons and previews. The ledger stays readable;
#: a longer reason carries no information, only risk (the chat journal's
#: rule, same number).
_MAX_REASON_CHARS = 200
_MAX_PREVIEW_CHARS = 240


class CallOutcome(str, Enum):
    """A journalled call's outcome, as the fold reads it.

    ``PROPOSED`` is not a terminal outcome: a record still holding it is
    a call the crash interrupted, which is exactly what the unknown-
    outcome classification (ADR-0050's D-6 rule) exists to handle.
    """

    PROPOSED = "PROPOSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One journalled call, folded from its rows.

    ``result`` is the decoded ``result_json`` — ``None`` when the row
    never recorded one (FAILED, PROPOSED) or when the blobbed original
    was not resolvable at fold time. ``result_sha256`` is always the
    attested digest, whatever the payload could carry.
    """

    seq: int
    method: str
    fingerprint: str
    outcome: CallOutcome
    params_sha256: str | None = None
    params_shape: str | None = None
    result_sha256: str | None = None
    result: Any = None
    error: str | None = None
    state_version: int | None = field(default=None)


def fingerprint(method: str, params: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of ``{method, params}``.

    This is ADR-0050's fingerprint, the identity of one call as the
    journal sees it: key order cannot change it, whitespace cannot
    change it, and a pipeline whose re-issued call differs in any
    parameter produces a different digest — which is the divergence
    signal, not a resync trigger.
    """
    return hashlib.sha256(
        canonical_json({"method": method, "params": dict(params)}).encode("utf-8")
    ).hexdigest()


def params_shape(params: Mapping[str, Any]) -> str:
    """A shape-only summary of a call's params, never their content.

    Strings render as ``<str len=N sha256=xxxxxxxx>`` — the coder
    journal's opaque-secret rule — so a prompt holding a credential
    leaves its length and a digest in the row and nothing else.
    Scalars render as-is (they cannot hide a secret and they orient a
    human); containers render their shape recursively; the whole preview
    is capped with an ellipsis so a bounded row stays bounded.
    """

    def render(value: Any) -> str:
        if isinstance(value, str):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
            return f"<str len={len(value)} sha256={digest}>"
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, int | float):
            return repr(value)
        if isinstance(value, Mapping):
            inner = ", ".join(f"{k}={render(v)}" for k, v in sorted(value.items()))
            return "{" + inner + "}"
        if isinstance(value, list | tuple):
            return "[" + ", ".join(render(v) for v in value) + "]"
        return f"<{type(value).__name__}>"

    rendered = render(dict(params))
    if len(rendered) > _MAX_PREVIEW_CHARS:
        rendered = rendered[: _MAX_PREVIEW_CHARS - 1] + "…"
    return rendered


def _bounded_reason(reason: str) -> str:
    return reason[:_MAX_REASON_CHARS]


class CallJournal:
    """Append the pipeline call-journal rows to one run's lineage.

    Constructed with the runtime's :class:`~paa.ledger.store.LedgerStore`
    and the run id — one run, one lineage, ``seq`` unique within it.
    ``attempt`` is always 0; the discriminator names the seq and the
    kind, so a redelivered append returns the existing event through the
    store's dedupe-then-seal transaction and the journal adds no second
    dedupe of its own.

    The optional blob store (N2.6 plumbing, shared with the coder
    journal) is the size-check target: an oversized payload — a
    ``result_json`` past the inline limit — is offloaded and the row
    keeps only the descriptor. Without a store an oversized payload
    raises rather than bloating the row.
    """

    def __init__(
        self,
        ledger: LedgerStore,
        run_id: uuid.UUID,
        *,
        blob_store: BlobStoreLike | None = None,
    ) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._blob_store = blob_store

    @property
    def run_id(self) -> uuid.UUID:
        return self._run_id

    def _blobbed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply the size check to one payload (the coder journal's rule)."""
        if not payload_requires_blob(payload):
            return payload
        if self._blob_store is None:
            raise ValueError(
                "CallJournal payload exceeded the inline limit with no blob store "
                "configured — refusing to bloat the ledger"
            )
        return maybe_blob_payload(payload, self._blob_store)

    async def _append(
        self, event_type: EventType, payload: dict[str, Any], discriminator: str
    ) -> LedgerEvent:
        return await self._ledger.append(
            LedgerEvent.create(
                self._run_id,
                event_type,
                attempt=0,
                discriminator=discriminator,
                payload=self._blobbed(payload),
            )
        )

    # -- run lifecycle ------------------------------------------------------

    async def run_started(
        self, pipeline: str, version: str, *, run_kind: str = "in-process"
    ) -> LedgerEvent:
        """Append PIPELINE_RUN_STARTED. Idempotent per run lineage."""
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "pipeline": pipeline,
            "version": version,
            "run_kind": run_kind,
        }
        return await self._append(
            EventType.PIPELINE_RUN_STARTED, payload, "run:STARTED"
        )

    async def run_completed(
        self, status: str, *, metrics: Mapping[str, Any] | None = None
    ) -> LedgerEvent:
        """Append PIPELINE_RUN_COMPLETED with the terminal status."""
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "status": status,
            "metrics": dict(metrics) if metrics else {},
        }
        return await self._append(
            EventType.PIPELINE_RUN_COMPLETED, payload, "run:COMPLETED"
        )

    async def run_failed(self, reason: str) -> LedgerEvent:
        """Append PIPELINE_RUN_FAILED with a bounded reason."""
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "reason": _bounded_reason(reason),
        }
        return await self._append(EventType.PIPELINE_RUN_FAILED, payload, "run:FAILED")

    async def run_diverged(
        self, seq: int, expected_fingerprint: str, actual_fingerprint: str
    ) -> LedgerEvent:
        """Append PIPELINE_RUN_DIVERGED, naming the offending call.

        The run stops here by rule (ADR-0050: the host never resyncs);
        this row is the stop's audit record, not a request anyone may
        answer. The discriminator is the run-scoped kind — one
        divergence closes the run, so a second is the same stop.
        """
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "seq": seq,
            "expected_fingerprint": expected_fingerprint,
            "actual_fingerprint": actual_fingerprint,
        }
        return await self._append(
            EventType.PIPELINE_RUN_DIVERGED, payload, "run:DIVERGED"
        )

    # -- calls ---------------------------------------------------------------

    async def call_proposed(
        self, seq: int, method: str, params: Mapping[str, Any]
    ) -> LedgerEvent:
        """Append PIPELINE_CALL_PROPOSED — the ledger-before-effect point.

        ``seq`` must be positive and new: re-proposing a seq this journal
        already wrote is a driver bug (the replay path answers from the
        record instead of re-proposing), and a non-positive seq cannot be
        monotonic. Both are refused before anything is appended.
        """
        if seq < 1:
            raise ValueError(f"CallJournal seq must be >= 1, got {seq}")
        params_sha = hashlib.sha256(
            canonical_json(dict(params)).encode("utf-8")
        ).hexdigest()
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "seq": seq,
            "method": method,
            "fingerprint": fingerprint(method, params),
            "params_sha256": params_sha,
            "params_shape": params_shape(params),
        }
        return await self._append(
            EventType.PIPELINE_CALL_PROPOSED, payload, f"call:{seq}:PROPOSED"
        )

    async def call_completed(
        self, seq: int, method: str, result: Any
    ) -> LedgerEvent:
        """Append PIPELINE_CALL_COMPLETED with the servable result.

        The result's canonical JSON rides the payload so replay can
        return the recorded value (ADR-0050: replay reads the record and
        never re-executes); its SHA-256 attests it. A result that cannot
        be canonically serialised raises before anything is appended —
        an unrecordable result is a result the journal cannot serve back,
        and pretending otherwise would make replay a lie.
        """
        result_json = canonical_json(result)
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "seq": seq,
            "method": method,
            "result_sha256": hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
            "result_json": result_json,
        }
        return await self._append(
            EventType.PIPELINE_CALL_COMPLETED, payload, f"call:{seq}:COMPLETED"
        )

    async def call_failed(self, seq: int, method: str, reason: str) -> LedgerEvent:
        """Append PIPELINE_CALL_FAILED. Never attests a result."""
        payload: dict[str, Any] = {
            "v": 1,
            "run_id": str(self._run_id),
            "seq": seq,
            "method": method,
            "reason": _bounded_reason(reason),
        }
        return await self._append(
            EventType.PIPELINE_CALL_FAILED, payload, f"call:{seq}:FAILED"
        )


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


#: Call rows the fold reads (the three per-call kinds). Run-lifecycle
#: rows are skipped by the same membership test without listing them.
_CALL_ROW_TYPES = frozenset(
    {
        EventType.PIPELINE_CALL_PROPOSED,
        EventType.PIPELINE_CALL_COMPLETED,
        EventType.PIPELINE_CALL_FAILED,
    }
)


def _safe_int(value: Any) -> int | None:
    """An int from a payload, or ``None``. ``True`` is not 1."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _safe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _decode_result(payload: Mapping[str, Any]) -> Any:
    """The recorded result, decoded, or ``None``.

    Total over malformed rows: a ``result_json`` that does not parse
    reads as absent (the sha remains the attestation); a blobbed payload
    resolves through the store when one was supplied, and a resolution
    failure degrades to ``None`` the same way.
    """
    raw = payload.get("result_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def fold_call_journal(
    events: Iterable[LedgerEvent],
    *,
    blob_store: BlobStoreLike | None = None,
) -> list[CallRecord]:
    """Fold one run lineage's events into call records, keyed by ``seq``.

    Total over any input — unknown event types, malformed payloads and
    foreign lineages fold without raising; a row without a usable seq is
    skipped (the same defensive rule every house fold states). Records
    return in seq order.

    The fold is **order-independent by precedence**, not by arrival: an
    outcome row (COMPLETED or FAILED) always beats a bare PROPOSED for
    the same seq, whatever order the reader produced the rows in, and a
    seq carrying both outcomes reads FAILED — the conservative reading
    of a contradictory record, matching the coder fold's "the failure is
    what the next reader must see". Well-ordered lineages (the only kind
    the journal writes) fold identically either way; the precedence rule
    is what makes the ordering property below true rather than hopeful.
    """
    _precedence = {CallOutcome.PROPOSED: 0, CallOutcome.COMPLETED: 1, CallOutcome.FAILED: 2}
    records: dict[int, CallRecord] = {}
    for event in events:
        etype = event.event_type
        if etype not in _CALL_ROW_TYPES:
            continue
        payload: Mapping[str, Any] = event.payload
        if is_blobbed_payload(dict(payload)) and blob_store is not None:
            try:
                payload = resolve_blobbed_payload(dict(payload), blob_store)
            except ValueError:
                # An unresolvable blob degrades to the stub's fields; the
                # sha stays the attestation either way.
                payload = event.payload
        seq = _safe_int(payload.get("seq"))
        if seq is None:
            continue
        method = _safe_str(payload.get("method")) or ""
        version = event.state_version
        prior = records.get(seq)
        if etype is EventType.PIPELINE_CALL_PROPOSED:
            candidate = CallRecord(
                seq=seq,
                method=method or (prior.method if prior else ""),
                fingerprint=_safe_str(payload.get("fingerprint"))
                or (prior.fingerprint if prior else ""),
                outcome=CallOutcome.PROPOSED,
                params_sha256=_safe_str(payload.get("params_sha256"))
                or (prior.params_sha256 if prior else None),
                params_shape=_safe_str(payload.get("params_shape"))
                or (prior.params_shape if prior else None),
                state_version=version,
            )
        elif etype is EventType.PIPELINE_CALL_COMPLETED:
            candidate = CallRecord(
                seq=seq,
                method=method or (prior.method if prior else ""),
                fingerprint=(prior.fingerprint if prior else ""),
                outcome=CallOutcome.COMPLETED,
                params_sha256=(prior.params_sha256 if prior else None),
                params_shape=(prior.params_shape if prior else None),
                result_sha256=_safe_str(payload.get("result_sha256")),
                result=_decode_result(payload),
                state_version=version,
            )
        else:
            candidate = CallRecord(
                seq=seq,
                method=method or (prior.method if prior else ""),
                fingerprint=(prior.fingerprint if prior else ""),
                outcome=CallOutcome.FAILED,
                params_sha256=(prior.params_sha256 if prior else None),
                params_shape=(prior.params_shape if prior else None),
                error=_safe_str(payload.get("reason")),
                state_version=version,
            )
        if prior is None or _precedence[candidate.outcome] >= _precedence[prior.outcome]:
            # Equal precedence (a redelivered kind that deduped in the
            # store) keeps the later row's fields — same outcome, newer
            # state version, nothing observable changes.
            records[seq] = candidate
    return [records[seq] for seq in sorted(records)]


async def read_call_journal(
    store: LedgerStore,
    run_id: uuid.UUID,
    *,
    blob_store: BlobStoreLike | None = None,
) -> list[CallRecord]:
    """Fold one run's call journal straight from the ledger."""
    events = await store.read_correlation(run_id)
    return fold_call_journal(events, blob_store=blob_store)
