"""Ledger event envelope, idempotency, and the tamper-evident hash chain.

The ledger is the runtime's only source of truth. Redis, in-process caches and
the filesystem are all derived views that may be rebuilt from these rows.

Three properties are enforced here:

1. **Idempotency** — an at-least-once transport may deliver the same logical
   event twice; the second write is suppressed rather than duplicated.
2. **Ordering** — every event carries a per-correlation monotonic
   ``state_version`` so replay is deterministic even if ``sequence_id`` gaps.
3. **Tamper evidence** — each event commits to its predecessor's digest,
   forming a hash chain that makes silent history edits detectable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from paa.core.types import ComplexityModality, CorrelationId, EventType, SessionId

__all__ = [
    "GENESIS_HASH",
    "LedgerEvent",
    "canonical_json",
    "compute_event_digest",
    "compute_idempotency_key",
]

#: The ``prev_hash`` of the first event in any correlation's chain.
GENESIS_HASH: str = "0" * 64


def canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing.

    Sorted keys, no insignificant whitespace, and non-ASCII preserved. Two
    structurally equal payloads must always produce byte-identical output,
    otherwise the hash chain would report false tampering.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_fallback,
    )


def _json_fallback(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    raise TypeError(f"{type(obj).__name__} is not JSON-serialisable in a ledger payload")


def compute_idempotency_key(
    correlation_id: CorrelationId | uuid.UUID,
    event_type: EventType,
    *,
    attempt: int = 0,
    discriminator: str | None = None,
) -> str:
    """Derive the deduplication key for one logical event.

    SPEC DEVIATION (docs/adr/0008). The RFC specifies
    ``SHA-256(correlation_id + event_type + state_version)`` under a global
    UNIQUE constraint. That makes a legitimate retry impossible: a task whose
    first ``EXECUTION_STARTED`` failed can never emit a second one, because the
    key collides and the append is rejected. Recovery would deadlock on exactly
    the tasks it exists to rescue.

    Two inputs are added to fix it:

    ``attempt``
        Bumped by the caller on each genuine retry, so retry *n* is a distinct
        event while a *redelivery* of attempt *n* is still suppressed.

    ``discriminator``
        Distinguishes same-typed events that are legitimately concurrent — e.g.
        ``EXECUTION_STARTED`` for step 3 vs step 4 of one plan. Typically the
        step index or a natural key.

    The payload is deliberately *excluded*: payloads routinely carry timestamps
    and durations, so hashing them would make every redelivery look novel and
    defeat deduplication entirely.
    """
    if attempt < 0:
        raise ValueError(f"attempt must be non-negative, got {attempt}")
    material = f"{correlation_id}|{event_type.value}|{attempt}|{discriminator or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_event_digest(
    *,
    prev_hash: str,
    correlation_id: CorrelationId | uuid.UUID,
    event_type: EventType,
    state_version: int,
    payload: dict[str, Any],
    recorded_at: datetime,
) -> str:
    """Digest binding an event to its predecessor.

    Includes ``prev_hash``, so altering any historical row invalidates every
    digest after it. Verified by :func:`paa.ledger.store.LedgerStore.verify_chain`.
    """
    material = canonical_json(
        {
            "prev": prev_hash,
            "correlation_id": str(correlation_id),
            "event_type": event_type.value,
            "state_version": state_version,
            "payload": payload,
            "recorded_at": recorded_at.astimezone(UTC).isoformat(),
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LedgerEvent(BaseModel):
    """One immutable state transition.

    Instances are frozen. Fields assigned by the store at append time
    (``sequence_id``, ``state_version``, ``prev_hash``, ``event_hash``) are
    ``None`` on a freshly constructed event and populated on the persisted copy
    the store returns.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- assigned by the store inside the append transaction ---------------
    sequence_id: int | None = None
    """Global monotonic position. ``None`` until persisted."""

    state_version: int | None = None
    """Per-correlation monotonic counter, starting at 1."""

    prev_hash: str | None = None
    event_hash: str | None = None

    # -- caller-supplied ---------------------------------------------------
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID
    """Lineage key. One task from request through commit shares one value."""

    session_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    """The ``event_id`` that directly caused this one. Enables the DoD's
    "explain why it chose an action" by walking the causal chain backwards."""

    event_type: EventType
    execution_mode: ComplexityModality = ComplexityModality.STANDARD
    agent_role: str | None = None
    allocated_worker_image: str = "paa/base_worker:v4.1"

    attempt: int = Field(default=0, ge=0)
    discriminator: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        """Naive timestamps silently break ordering across DST and restarts."""
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        return self

    @field_serializer("recorded_at")
    def _serialize_ts(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @property
    def idempotency_key(self) -> str:
        """This event's deduplication key."""
        return compute_idempotency_key(
            self.correlation_id,
            self.event_type,
            attempt=self.attempt,
            discriminator=self.discriminator,
        )

    @property
    def is_terminal(self) -> bool:
        from paa.core.types import TERMINAL_EVENTS

        return self.event_type in TERMINAL_EVENTS

    def sealed(self, *, sequence_id: int, state_version: int, prev_hash: str) -> LedgerEvent:
        """Return the persisted form, with chain fields computed.

        Called by the store inside the append transaction once the sequence and
        version are known.
        """
        digest = compute_event_digest(
            prev_hash=prev_hash,
            correlation_id=self.correlation_id,
            event_type=self.event_type,
            state_version=state_version,
            payload=self.payload,
            recorded_at=self.recorded_at,
        )
        return self.model_copy(
            update={
                "sequence_id": sequence_id,
                "state_version": state_version,
                "prev_hash": prev_hash,
                "event_hash": digest,
            }
        )

    def verify_digest(self) -> bool:
        """Recompute this event's digest and compare it to the stored value."""
        if self.event_hash is None or self.prev_hash is None or self.state_version is None:
            return False
        expected = compute_event_digest(
            prev_hash=self.prev_hash,
            correlation_id=self.correlation_id,
            event_type=self.event_type,
            state_version=self.state_version,
            payload=self.payload,
            recorded_at=self.recorded_at,
        )
        return expected == self.event_hash

    @classmethod
    def create(
        cls,
        correlation_id: CorrelationId | uuid.UUID,
        event_type: EventType,
        *,
        session_id: SessionId | uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LedgerEvent:
        """Ergonomic constructor for the common append path."""
        return cls(
            correlation_id=correlation_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )


#: The 8 KB inline limit for one ledger payload (N2.6). A payload whose
#: canonical form is larger than this is stored as a cold-lake blob and the
#: row keeps only a descriptor. The value is bytes of the *canonical* form,
#: not ``len(payload)``: the hash chain commits to those bytes, so the size
#: the chain protects is the size measured on them.
BLOB_THRESHOLD_BYTES: Final = 8192

#: Human-orientation characters kept in a descriptor. Bounded so that a
#: blobbed row stays small no matter how large the original was; the
#: preview is never used for reconstruction, only for reading.
BLOB_PREVIEW_CHARS: Final = 200

#: The top-level key holding the blob descriptor in a stub payload.
#: Payloads must not use this key for any other purpose: a stub-shaped
#: payload is a stub, and any other use is rejected by the resolver.
_BLOB_KEY: Final = "blob"


class BlobStoreLike(Protocol):
    """The narrow surface the blob helpers need from a cold-lake store.

    The real stores (:class:`~paa.storage.coldlake.cas.ContentAddressedStore`
    and the S3 backend) satisfy it structurally; the ledger depends on this
    two-method shape rather than importing the store classes, so a minimal
    install that lacks the S3 extra still imports this module. ``put``
    returns ``Any`` deliberately: callers need only the two facts the
    descriptor pins (the digest and the raw size), and those are recomputed
    from ``data`` here, so the exact ``BlobRef`` type — which both
    implementations mint identically — need not be imported to name the
    return. ``get`` is the verification point: the stores verify the digest
    on read and raise their own ``Corrupt`` / ``NotFound`` on mismatch or
    absence, which this module lets propagate unchanged.
    """

    def put(self, data: bytes, *, compress: bool = True) -> Any: ...

    def get(self, digest: str) -> bytes: ...


def payload_size_bytes(payload: dict[str, Any]) -> int:
    """Bytes of the canonical form of ``payload``.

    This is the size the hash chain protects, and therefore the size the
    threshold is measured on: ``canonical_json`` with sorted keys and no
    whitespace, UTF-8 encoded. Two payloads that hash identically measure
    identically, which is what makes the threshold reproducible.
    """
    return len(canonical_json(payload).encode("utf-8"))


def payload_requires_blob(payload: dict[str, Any]) -> bool:
    """Whether ``payload`` exceeds the inline limit."""
    return payload_size_bytes(payload) > BLOB_THRESHOLD_BYTES


def _preview_of(data: bytes) -> str:
    """The first :data:`BLOB_PREVIEW_CHARS` of ``data`` as text.

    Decoding is lossy (``errors="replace"``) because the preview is a
    human hint over bytes that may be binary: a non-UTF-8 blob still gets
    a bounded preview rather than an exception. The preview is never the
    record — reconstruction always re-reads the blob — so loss here cannot
    corrupt a read-back.
    """
    text = data.decode("utf-8", errors="replace")
    if len(text) <= BLOB_PREVIEW_CHARS:
        return text
    return text[: BLOB_PREVIEW_CHARS - 1] + "…"


def _descriptor_for_bytes(data: bytes) -> dict[str, Any]:
    """The descriptor pinning one blob of ``data``.

    The digest is over the *raw* bytes, never a compressed frame, matching
    the cold lake's own rule: the address of a payload does not depend on
    the compression level in effect when it was written.
    """
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "preview": _preview_of(data),
    }


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _blob_descriptor(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The descriptor of a blobbed payload, or ``None`` if not blobbed.

    A payload is blobbed only in exactly the stub shape this module writes:
    the two keys ``v`` and ``blob``, with ``blob`` a dict of exactly the
    three descriptor fields of the right types. The strictness is the
    collision guard: a payload that merely happens to carry a ``blob``
    field of some other shape is an ordinary payload, not a stub, and is
    returned unchanged rather than fetched.
    """
    if set(payload) != {"v", _BLOB_KEY}:
        return None
    version = payload["v"]
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    raw = payload[_BLOB_KEY]
    if not isinstance(raw, dict):
        return None
    if set(raw) != {"sha256", "size_bytes", "preview"}:
        return None
    if not _is_sha256_hex(raw["sha256"]):
        return None
    size = raw["size_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    preview = raw["preview"]
    if not isinstance(preview, str) or len(preview) > BLOB_PREVIEW_CHARS:
        return None
    return raw


def is_blobbed_payload(payload: dict[str, Any]) -> bool:
    """Whether ``payload`` is a well-formed blobbed stub."""
    return _blob_descriptor(payload) is not None


def store_blob_bytes(data: bytes, blob_store: BlobStoreLike) -> dict[str, Any]:
    """Store ``data`` and return its descriptor.

    The store's own digest is checked against the digest recomputed here:
    a store that addressed different bytes than it was given must fail
    loudly rather than pin the wrong address in the ledger. A store that
    returns no reference object is accepted (test doubles); a store whose
    reference disagrees is not.
    """
    descriptor = _descriptor_for_bytes(data)
    ref: Any = blob_store.put(data)
    ref_sha: Any = getattr(ref, "sha256", None)
    if ref_sha is not None and ref_sha != descriptor["sha256"]:
        raise ValueError(
            "blob store addressed different bytes than were written: "
            f"expected {descriptor['sha256']}, store returned {ref_sha!r}"
        )
    return descriptor


def read_blob_bytes(descriptor: dict[str, Any], blob_store: BlobStoreLike) -> bytes:
    """Read the blob named by ``descriptor``, verified before return.

    Two independent checks run before any byte is handed back: the blob
    store's own digest verification (``get`` raises ``Corrupt`` on a
    mismatch and propagates unchanged), and the size check below recomputed
    here from the fetched bytes. A ``size_bytes`` that disagrees with the
    fetched length is a ``ValueError`` even when the digest matches, so a
    descriptor edited to lie about the size cannot pass. Malformed
    descriptors raise ``ValueError`` before any fetch is attempted.
    """
    if set(descriptor) != {"sha256", "size_bytes", "preview"}:
        raise ValueError(f"malformed blob descriptor keys: {sorted(descriptor)}")
    sha = descriptor["sha256"]
    if not _is_sha256_hex(sha):
        raise ValueError(f"malformed blob descriptor sha256: {sha!r}")
    size = descriptor["size_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError(f"malformed blob descriptor size_bytes: {size!r}")
    preview = descriptor["preview"]
    if not isinstance(preview, str) or len(preview) > BLOB_PREVIEW_CHARS:
        raise ValueError(f"malformed blob descriptor preview of length {len(preview)}")
    data = blob_store.get(sha)
    if len(data) != size:
        raise ValueError(
            f"blob size mismatch for {sha}: descriptor pins {size} bytes, "
            f"store returned {len(data)}"
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha:
        raise ValueError(f"blob sha256 mismatch: expected {sha}, computed {actual}")
    return data


def maybe_blob_payload(
    payload: dict[str, Any], blob_store: BlobStoreLike
) -> dict[str, Any]:
    """Return ``payload`` unchanged, or a blobbed stub when oversized.

    Small payloads (``<= BLOB_THRESHOLD_BYTES``) are returned as a shallow
    copy, equal to the input, with nothing stored. Oversized payloads are
    encoded once as canonical bytes, stored as a blob, and replaced by the
    two-key stub ``{"v": 1, "blob": descriptor}``. The outer ``v`` is the
    blob-envelope version (currently ``1``): readers that need the
    original payload's fields must resolve the blob first, because the
    stub carries none of them. An already-blobbed payload is returned
    unchanged (its stub is already small); a stub-shaped payload with a
    malformed descriptor raises rather than being re-stored.
    """
    if is_blobbed_payload(payload):
        return dict(payload)
    if set(payload) == {"v", _BLOB_KEY}:
        raise ValueError("blobbed stub with a malformed descriptor; refusing to re-store")
    if not payload_requires_blob(payload):
        return dict(payload)
    data = canonical_json(payload).encode("utf-8")
    descriptor = store_blob_bytes(data, blob_store)
    return {"v": 1, _BLOB_KEY: descriptor}


def resolve_blobbed_payload(
    payload: dict[str, Any], blob_store: BlobStoreLike
) -> dict[str, Any]:
    """Return the original payload, verified, for a blobbed stub.

    Non-blobbed payloads are returned as a shallow copy, unchanged; no
    fetch is attempted. Blobbed stubs are fetched and verified by
    :func:`read_blob_bytes`, then parsed and checked for canonical form:
    the decoded bytes must re-encode to themselves, so a blob that holds
    the right *dict* in non-canonical bytes (different key order or
    whitespace) is still rejected — the digest protects bytes, and only
    those bytes are accepted as the original. Replay and verification
    must go through this function; a caller that reads ``payload["blob"]``
    directly reads unverified bytes.
    """
    descriptor = _blob_descriptor(payload)
    if descriptor is None:
        if set(payload) == {"v", _BLOB_KEY}:
            raise ValueError("blobbed stub with a malformed descriptor")
        return dict(payload)
    data = read_blob_bytes(descriptor, blob_store)
    try:
        text = data.decode("utf-8")
        parsed: Any = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"blobbed payload did not hold JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("blobbed payload did not hold a JSON object")
    if canonical_json(parsed).encode("utf-8") != data:
        raise ValueError("blobbed payload bytes are not the canonical form")
    return parsed


__all__.extend(
    [
        "BLOB_PREVIEW_CHARS",
        "BLOB_THRESHOLD_BYTES",
        "BlobStoreLike",
        "is_blobbed_payload",
        "maybe_blob_payload",
        "payload_requires_blob",
        "payload_size_bytes",
        "read_blob_bytes",
        "resolve_blobbed_payload",
        "store_blob_bytes",
    ]
)
