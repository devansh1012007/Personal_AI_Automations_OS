"""The effect record: kind, scope, owner, and compensation declared as data.

README's effect-ownership graph asks "which scope created each resource or
side effect, and how can it be disposed or reconciled?" (the Temporal
composability section). This module is the answer's data shape — the
smallest honest slice of the object model the VISION appendix proposes
(``docs/VISION.md`` §"Represent each effect as a managed lifecycle
record"): ownership, operation identity, and recovery information are
explicit; nothing here is executable.

Three rules make the record honest rather than aspirational:

1. **The compensation path is data, never code.** A callable smuggles a
   judgment ("how to undo this") into the ledger's read path; data can be
   inspected, replayed, and refused. ``file_write``/``file_edit`` carry
   ``{"action": "remove" | "restore", "path", "prior_sha256",
   "prior_bytes_ref"}`` — remove for a creation (no prior content to put
   back), restore for a modification (the prior bytes live in the
   content-addressed blob store, attested by ``prior_sha256``).
   ``irreversible`` carries none: an email that was sent cannot be unsent,
   and a record that pretended otherwise would be the dangerous kind of
   wrong.
2. **The effect id is deterministic from lineage + seq.** The id is
   ``sha256(canonical "lineage|seq")`` — the same call always mints the
   same id, so a compensation row that references it names exactly one
   effect without a join table, and a re-derived spec cannot drift from
   its recorded one. The v1 owner is a ``PIPELINE_CALL_COMPLETED`` row
   (ADR-0054); the owner reference is ``(lineage, seq, event_type)``,
   which resolves to exactly one row through the journal's ADR-0008
   discriminator ``call:<seq>:COMPLETED``.
3. **Payload discipline is the journal's, restated.** The serialized form
   carries ``v`` (currently ``1``), closed-vocabulary kinds and actions,
   a workspace-relative path bounded in length, and digests — never file
   content, never free text. Reading is total: a malformed ``effect``
   field reads as *no effect* (the row stays in the ledger, the hash chain
   still attests it), matching the house rule that a fold must survive any
   input.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "CompensationAction",
    "CompensationSpec",
    "EffectKind",
    "EffectSpec",
    "OwnerRef",
    "effect_id_for",
]

#: Schema version of the serialized ``effect`` field (ADR-0046's v-field
#: rule: writers always write the current version, readers branch on it).
EFFECT_PAYLOAD_VERSION = 1

#: The effect-owning event vocabulary v1 knows. The owner reference names
#: its event type explicitly so a reader can resolve the row without
#: guessing which journal wrote it; widening the set (TOOL_CALL_COMPLETED)
#: is a v2 change that arrives with its own producer.
OWNER_EVENT_TYPES = frozenset({"PIPELINE_CALL_COMPLETED"})

#: Longest workspace-relative path an effect record may carry. A bound
#: keeps a pathological (or hostile) path from bloating the payload past
#: the blob threshold, where the effect would hide inside a blob every
#: reader without a store would have to skip.
_MAX_PATH_CHARS = 1024


class EffectKind(str, Enum):
    """What kind of side effect a journalled call produced.

    ``file_write`` and ``file_edit`` are the v1 compensable kinds: a
    workspace file was created/replaced or edited in place, and the
    compensation restores prior bytes or removes the creation. Both map to
    the same compensation vocabulary — the distinction is recorded because
    the *ownership graph* should say what happened, not merely what can be
    undone. ``irreversible`` is the honest marker for everything else: an
    external action with no declared compensation, reported and never
    "fixed".
    """

    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    IRREVERSIBLE = "irreversible"


class CompensationAction(str, Enum):
    """The one-word verb of a compensation, as data.

    ``remove`` — the effect created the file; compensating removes it.
    ``restore`` — the effect modified an existing file; compensating puts
    the prior bytes back (fetched from the blob address, verified against
    the recorded digest).
    """

    REMOVE = "remove"
    RESTORE = "restore"


_COMPENSABLE_KINDS = frozenset({EffectKind.FILE_WRITE, EffectKind.FILE_EDIT})


def effect_id_for(lineage: str | uuid.UUID, seq: int) -> str:
    """The deterministic effect id for one journalled call.

    ``sha256("<lineage>|<seq>")`` over the canonical hyphenated spelling of
    the lineage uuid, so a caller holding either spelling mints the same
    id. Two calls on one lineage cannot share a seq (the journal refuses a
    re-proposed seq), and two lineages cannot share a uuid, which is what
    makes the id stable *and* unique without a counter or a mint table.
    """
    canonical = str(lineage) if isinstance(lineage, uuid.UUID) else str(uuid.UUID(lineage))
    if seq < 1:
        raise ValueError(f"effect owner seq must be >= 1, got {seq}")
    material = f"{canonical}|{seq}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_relative_path(path: str) -> None:
    """Reject paths that are not bounded workspace-relative posix paths.

    The executor jails every compensation path against the workspace root
    anyway (:func:`paa.sandbox.base.assert_inside_workspace`) — this is
    the data-level pre-validation, so a record that could never be
    compensated honestly is refused at construction rather than
    discovered at rollback time. A path that escapes (absolute, ``..``,
    backslash, NUL) or is unbounded is not a compensation path; it is a
    claim this record cannot keep.
    """
    if not path:
        raise ValueError("compensation path must not be empty")
    if len(path) > _MAX_PATH_CHARS:
        raise ValueError(
            f"compensation path exceeds {_MAX_PATH_CHARS} chars, got {len(path)}"
        )
    if path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"compensation path must be workspace-relative, got {path!r}")
    if "\\" in path:
        raise ValueError(f"compensation path must be posix, got {path!r}")
    if "\x00" in path:
        raise ValueError("compensation path contains a NUL byte")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(
            f"compensation path must be normalized (no '', '.' or '..' segments), "
            f"got {path!r}"
        )


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


@dataclass(frozen=True, slots=True)
class CompensationSpec:
    """How to compensate one file effect, as data (never a callable).

    ``path`` is workspace-relative and normalized; the rollback resolves
    it against the workspace root the operator names and jails it there.
    ``prior_sha256`` attests the prior content and ``prior_bytes_ref`` is
    its address in the content-addressed blob store — the digest proves
    what the bytes are, the address says where to fetch them; the executor
    verifies the fetched bytes against the digest before writing.
    """

    action: CompensationAction
    path: str
    prior_sha256: str | None = None
    prior_bytes_ref: str | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        if self.action is CompensationAction.REMOVE and (
            self.prior_sha256 is not None or self.prior_bytes_ref is not None
        ):
            # Removing compensates a *creation*: there was no prior file,
            # so there is nothing to restore and recording a digest would
            # be a claim about content that never existed.
            raise ValueError("remove compensation records no prior content")
        if self.action is CompensationAction.RESTORE:
            # Restoring requires prior content: a digest to verify against
            # and an address to fetch from. Either missing means the
            # record claims a restore it cannot perform.
            if not _is_sha256_hex(self.prior_sha256):
                raise ValueError("restore compensation requires a prior_sha256 digest")
            if not _is_sha256_hex(self.prior_bytes_ref):
                raise ValueError("restore compensation requires a prior_bytes_ref address")

    def to_payload(self) -> dict[str, Any]:
        """The serialized form (ADR-0046 payload discipline: data, no prose)."""
        return {
            "action": self.action.value,
            "path": self.path,
            "prior_sha256": self.prior_sha256,
            "prior_bytes_ref": self.prior_bytes_ref,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> CompensationSpec | None:
        """Rebuild a compensation from its serialized form, or ``None``.

        Total over any input — a malformed field reads as *no
        compensation* and the caller (the registry) reports the effect as
        it would an irreversible one, never crashes on it.
        """
        if not isinstance(payload, dict):
            return None
        try:
            action = CompensationAction(payload["action"])
        except (KeyError, ValueError):
            return None
        path = payload.get("path")
        prior_sha = payload.get("prior_sha256")
        prior_ref = payload.get("prior_bytes_ref")
        if not isinstance(path, str):
            return None
        # The prior fields are nullable (a remove records none), but a
        # non-null value must be a digest: anything else is a record this
        # reader cannot honour.
        for prior in (prior_sha, prior_ref):
            if prior is not None and not _is_sha256_hex(prior):
                return None
        try:
            return cls(action, path, prior_sha, prior_ref)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class OwnerRef:
    """The journalled call that created the effect.

    ``(lineage, seq, event_type)`` resolves to exactly one ledger row
    through the writing journal's ADR-0008 discriminator
    (``call:<seq>:COMPLETED`` for the pipeline journal), so the owner is a
    *reference*, not a copy: the row's own envelope carries the event id,
    the state version and the hash chain link.
    """

    lineage: str
    seq: int
    event_type: str

    def __post_init__(self) -> None:
        if self.event_type not in OWNER_EVENT_TYPES:
            raise ValueError(
                f"unknown owner event type {self.event_type!r}; "
                f"expected one of {sorted(OWNER_EVENT_TYPES)}"
            )
        if self.seq < 1:
            raise ValueError(f"owner seq must be >= 1, got {self.seq}")
        # A lineage that is not a uuid cannot name a ledger row; refuse it
        # here rather than letting a typo'd scope read back empty.
        uuid.UUID(self.lineage)

    def __str__(self) -> str:
        return f"{self.lineage}/call:{self.seq}"


@dataclass(frozen=True, slots=True)
class EffectSpec:
    """One managed effect: who owns it, what it did, how it can be undone.

    The frozen record is the unit the registry indexes and the rollback
    walks. ``scope`` is the ownership boundary (README's effect-ownership
    graph: which scope created this side effect) — in v1 the scope is the
    effect-bearing call's own lineage, and the invariant is checked at
    construction; the split between a task scope and a run-lineage owner
    becomes real when ``TOOL_CALL_*`` effects arrive and the field is
    already there to hold it.
    """

    effect_id: str
    kind: EffectKind
    scope: str
    owner: OwnerRef
    compensation: CompensationSpec | None = None

    def __post_init__(self) -> None:
        # Determinism is the invariant: an id that does not derive from
        # the owner's (lineage, seq) would silently break the
        # compensation-reference join and the crash-resume derivation.
        expected = effect_id_for(self.owner.lineage, self.owner.seq)
        if self.effect_id != expected:
            raise ValueError(
                f"effect_id {self.effect_id!r} is not the deterministic id for "
                f"owner {self.owner} (expected {expected!r})"
            )
        if self.owner.lineage != self.scope:
            raise ValueError(
                "v1 requires the owner lineage and the scope to coincide "
                f"(owner {self.owner.lineage}, scope {self.scope}); a split scope "
                "is a v2 change (ADR-0054)"
            )
        if self.kind is EffectKind.IRREVERSIBLE and self.compensation is not None:
            raise ValueError("an irreversible effect declares no compensation")
        if self.kind in _COMPENSABLE_KINDS and self.compensation is None:
            raise ValueError(
                f"a {self.kind.value} effect requires a compensation spec; "
                "declare EffectKind.IRREVERSIBLE if none exists"
            )

    @classmethod
    def for_call(
        cls,
        lineage: str | uuid.UUID,
        seq: int,
        kind: EffectKind,
        compensation: CompensationSpec | None = None,
    ) -> EffectSpec:
        """Mint the effect for the call about to be journalled complete.

        The one constructor call sites use: it derives the id and builds
        the owner reference, so a caller cannot mistype either half of
        the determinism invariant. The scope is the call's lineage (the
        v1 rule above).
        """
        canonical = str(lineage) if isinstance(lineage, uuid.UUID) else str(uuid.UUID(lineage))
        return cls(
            effect_id=effect_id_for(canonical, seq),
            kind=kind,
            scope=canonical,
            owner=OwnerRef(canonical, seq, "PIPELINE_CALL_COMPLETED"),
            compensation=compensation,
        )

    def to_payload(self) -> dict[str, Any]:
        """The serialized ``effect`` field (versioned; data, never code)."""
        return {
            "v": EFFECT_PAYLOAD_VERSION,
            "id": self.effect_id,
            "kind": self.kind.value,
            "scope": self.scope,
            "owner": {
                "lineage": self.owner.lineage,
                "seq": self.owner.seq,
                "event_type": self.owner.event_type,
            },
            "compensation": (
                self.compensation.to_payload() if self.compensation is not None else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> EffectSpec | None:
        """Rebuild an effect from its serialized form, or ``None``.

        Total over any input (the house fold rule): a payload that is not
        a well-formed v1 effect record reads as *no effect* — the row
        remains in the ledger and the hash chain still attests it. A
        record whose fields fail the construction invariants is equally
        ``None``: a compensation path that cannot be honoured must not
        surface as a compensable effect.
        """
        if not isinstance(payload, dict):
            return None
        if payload.get("v") != EFFECT_PAYLOAD_VERSION:
            return None
        if not isinstance(payload.get("id"), str):
            return None
        try:
            kind = EffectKind(payload["kind"])
        except (KeyError, ValueError):
            return None
        scope = payload.get("scope")
        owner_raw = payload.get("owner")
        if not isinstance(scope, str) or not isinstance(owner_raw, dict):
            return None
        try:
            owner = OwnerRef(
                lineage=owner_raw["lineage"],
                seq=owner_raw["seq"],
                event_type=owner_raw["event_type"],
            )
        except (KeyError, TypeError, ValueError):
            return None
        compensation = CompensationSpec.from_payload(payload.get("compensation"))
        if payload.get("compensation") is not None and compensation is None:
            # A compensation field that failed to parse is a broken
            # promise, not an absence — refuse the whole record rather
            # than let a restore read as "nothing to compensate".
            return None
        try:
            return cls(
                effect_id=payload["id"],
                kind=kind,
                scope=scope,
                owner=owner,
                compensation=compensation,
            )
        except ValueError:
            return None
