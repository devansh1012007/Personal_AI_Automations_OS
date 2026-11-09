"""Mandate v0 — who requested this work, and under which policy (N2.4).

A **mandate** is the recorded authority for one task or coder session: who
asserted the request, on what basis, and which effective policy it was
granted under. Tasks used to be submitted with none of that written down,
which made two questions unanswerable: "who asked for this?" and "under
which policy did it run?".

Three properties shape the design, and all three exist to keep the record
honest:

**The identity is asserted, never authority.** ``requester_asserted`` carries
the ``-asserted`` suffix on purpose: the whole value is what the caller
*claims*, recorded so the ledger can say who claimed it — not so a reader
can quote it as provenance. ``identity_assurance`` is ``"asserted"`` for
the same reason. The day a second principal exists, authentication makes
this an authority record; until then it is a claim with a permanent name.

**Basis is never collapsed.** ``explicit`` (a caller asserted a mandate),
``inferred`` (derived from an earlier grant), and ``implicit`` (the runtime
proceeded with no mandate at all) are three different facts. ``paa doctor``
prints them as three separate counts and never as one coverage percentage —
folding implicit into coverage would report "everything is authorised"
about a ledger in which nothing was.

**The policy version is content-addressed.** ``policy_version`` is the
SHA-256 of the canonical effective policy (mode, risk thresholds, strict
flag) plus a human label. A policy change cannot drift silently: an event
written under an older hash is visibly an older policy, and a mandate whose
hash no longer matches the effective policy is refused at submit time
rather than applied.

The persisted form is the ADR-0046 v1 payload (``MANDATE_GRANTED`` /
``MANDATE_REFUSED``), not a column: the mandate travels in the ledger
payload so the schema-parity lock is not engaged and the record is
hash-chained like every other event. Every mandate payload carries ``v=1``
per ADR-0046's forward-compatibility policy.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from paa.config import PolicySettings
from paa.core.types import EventType, PermissionMode
from paa.ledger.events import LedgerEvent, canonical_json
from paa.models.base import redact

__all__ = [
    "MANDATE_SCHEMA_VERSION",
    "Mandate",
    "MandateBasis",
    "MandateCounts",
    "PolicyVersion",
    "RequesterAsserted",
    "Scope",
    "count_bases",
    "effective_policy_version",
    "explicit_mandate",
    "head_mandate_summary",
    "implicit_mandate",
    "require_explicit_mandate",
]

#: Schema version stamped as ``v`` on every mandate payload (ADR-0046).
#: Bumping it is a change of record: readers branch on ``v``, and the golden
#: tests pin the v1 shape under it.
MANDATE_SCHEMA_VERSION: int = 1


class MandateBasis(str, enum.Enum):
    """How a mandate came to exist.

    The three values are three different facts and are counted separately
    everywhere they are reported (:func:`count_bases`, ``paa doctor``). An
    ``implicit`` mandate is not a weaker ``explicit`` one; it is evidence
    that no authority was recorded at all.
    """

    EXPLICIT = "explicit"
    INFERRED = "inferred"
    IMPLICIT = "implicit"


@dataclass(frozen=True, slots=True)
class RequesterAsserted:
    """The claimed requester of a task — an assertion, not provenance.

    ``kind`` names the asserting surface (``cli``, ``api``, ``coder``,
    ``runtime`` for an implicit mandate); ``id`` names who the surface
    claims asked. Neither value is authenticated, and neither may be quoted
    as authority — see the module docstring.
    """

    kind: str
    id: str

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.id.strip():
            raise ValueError(
                "requester_asserted needs a non-empty kind and id; "
                "an anonymous requester is an implicit mandate, not an asserted one"
            )


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    """One effective policy, identified by content.

    ``sha256`` is the SHA-256 of the canonical effective policy (sorted
    keys, no whitespace — :func:`paa.ledger.events.canonical_json`, frozen
    for the same reason every payload digest trusts it); ``label`` is the
    human-readable mode name it was computed under.
    """

    sha256: str
    label: str


@dataclass(frozen=True, slots=True)
class Scope:
    """What the mandate covers.

    ``workspace`` is the jailed tree the work may touch (``None`` when no
    single tree bounds it); ``permission_mode`` is the coder/task policy
    posture the grant was made under; ``budget`` is the step budget the
    grant covers (``None`` when unbounded). Kept to three fields: anything
    narrower than the work it describes is a claim the ledger cannot defend.
    """

    workspace: str | None = None
    permission_mode: str | None = None
    budget: int | None = None

    def __post_init__(self) -> None:
        if self.budget is not None and self.budget < 0:
            raise ValueError(f"mandate budget must be non-negative, got {self.budget}")


@dataclass(frozen=True, slots=True)
class Mandate:
    """The authority record for one task or coder session.

    Frozen: the same value must describe the grant at ``MANDATE_GRANTED``
    time and any later refusal of it; a change is a new mandate, never an
    in-place edit after the fact.
    """

    requester_asserted: RequesterAsserted
    basis: MandateBasis
    policy_version: PolicyVersion
    scope: Scope = Scope()
    identity_assurance: Literal["asserted"] = "asserted"
    """Always ``"asserted"`` in v0 — see the module docstring. There is no
    other value to offer: without a second principal, authentication has
    nothing to authenticate, and a v0 claim otherwise would be a lie the
    ledger cannot defend."""

    def __post_init__(self) -> None:
        if self.identity_assurance != "asserted":
            raise ValueError(
                "identity_assurance must be 'asserted' in mandate v0 "
                "(assertions only, never authority); got "
                f"{self.identity_assurance!r}"
            )

    def granted_payload(self, *, session_id: str | None = None) -> dict[str, Any]:
        """The ADR-0046 v1 ``MANDATE_GRANTED`` payload for this mandate.

        ``session_id`` is the coder session's id, or ``None`` for a
        task-level mandate. Free-form strings are scrubbed through
        :func:`paa.models.base.redact` before they reach the ledger: a
        requester id or workspace string that happens to be secret-shaped
        must not be persisted raw.
        """
        return {
            "v": MANDATE_SCHEMA_VERSION,
            "session_id": session_id,
            "requester_asserted": {
                "kind": redact(self.requester_asserted.kind),
                "id": redact(self.requester_asserted.id),
            },
            "identity_assurance": self.identity_assurance,
            "basis": self.basis.value,
            "policy_version": self.policy_version.sha256,
            "policy_label": self.policy_version.label,
            "scope": {
                "workspace": redact(self.scope.workspace)
                if self.scope.workspace is not None
                else None,
                "permission_mode": self.scope.permission_mode,
                "budget": self.scope.budget,
            },
        }

    def refused_payload(self, *, session_id: str | None, reason: str) -> dict[str, Any]:
        """The ADR-0046 v1 ``MANDATE_REFUSED`` payload for this mandate.

        A refusal names who was refused and why; it carries no scope and no
        basis, because no work proceeded under it — a refused mandate is
        evidence of a boundary, not of authority.
        """
        return {
            "v": MANDATE_SCHEMA_VERSION,
            "session_id": session_id,
            "requester_asserted": {
                "kind": redact(self.requester_asserted.kind),
                "id": redact(self.requester_asserted.id),
            },
            "reason": reason,
            "policy_version": self.policy_version.sha256,
        }


def require_explicit_mandate(policy: PolicySettings) -> bool:
    """Whether this policy refuses every non-explicit mandate.

    The configured flag (``PAA_POLICY__REQUIRE_EXPLICIT_MANDATE``, default
    false) requests strictness; ``LOCKDOWN`` forces it — an air-gap posture
    cannot proceed on an implicit grant whatever the flag says.

    The flag itself is read through ``getattr`` with the documented default
    so this module keeps working against a ``PolicySettings`` that predates
    the field; once the field lands the read is the field, with the same
    effective value. (Tracked as the N2.4 deferred dependency on
    ``src/paa/config.py``.)
    """
    if policy.mode is PermissionMode.LOCKDOWN:
        return True
    return bool(getattr(policy, "require_explicit_mandate", False))


def _effective_policy(policy: PolicySettings) -> dict[str, Any]:
    """The canonical effective policy the version hashes over.

    Canonical means JSON-deterministic: sorted keys and no whitespace (see
    :func:`canonical_json`), so two equal policies hash equal regardless of
    how either was constructed. The *effective* values are hashed, not the
    raw settings — strictness is the forced-or-configured value, because
    that is what actually gates work.
    """
    return {
        "mode": policy.mode.value,
        "anti_goal_threshold": policy.anti_goal_threshold,
        "auto_confidence_floor": policy.auto_confidence_floor,
        "always_gate_risk_profile": policy.always_gate_risk_profile,
        "latency_budget_ms": policy.latency_budget_ms,
        "require_explicit_mandate": require_explicit_mandate(policy),
    }


def effective_policy_version(policy: PolicySettings) -> PolicyVersion:
    """The content address of the effective policy."""
    digest = hashlib.sha256(canonical_json(_effective_policy(policy)).encode()).hexdigest()
    return PolicyVersion(sha256=digest, label=policy.mode.value)


def explicit_mandate(
    *,
    kind: str,
    requester_id: str,
    policy: PolicySettings,
    workspace: str | None = None,
    permission_mode: str | None = None,
    budget: int | None = None,
) -> Mandate:
    """An explicitly asserted mandate, stamped with the effective policy.

    Used by the CLI, the API and ``paa agent``: the caller asserts who
    requested the work, and the *server* (never the caller) stamps the
    effective policy version, so a caller cannot forge which policy a
    mandate was granted under.
    """
    return Mandate(
        requester_asserted=RequesterAsserted(kind=kind, id=requester_id),
        basis=MandateBasis.EXPLICIT,
        policy_version=effective_policy_version(policy),
        scope=Scope(
            workspace=workspace,
            permission_mode=permission_mode or policy.mode.value,
            budget=budget,
        ),
    )


def implicit_mandate(policy: PolicySettings) -> Mandate:
    """The mandate a submission runs under when none was asserted.

    The runtime proceeds, but the ledger says so in the basis: an implicit
    mandate is counted separately from explicit grants and is never
    backfilled as one. The requester is the runtime itself — the only
    honest assertion available.
    """
    return Mandate(
        requester_asserted=RequesterAsserted(kind="runtime", id="implicit"),
        basis=MandateBasis.IMPLICIT,
        policy_version=effective_policy_version(policy),
        scope=Scope(permission_mode=policy.mode.value),
    )


@dataclass(frozen=True, slots=True)
class MandateCounts:
    """Mandate basis counts, reported as three facts and never folded.

    ``explicit`` + ``inferred`` + ``implicit`` is the number of granted
    mandates whose basis could be read; a granted mandate whose payload
    carries no recognised basis is no evidence under v1 and is counted
    nowhere — inventing a bucket for it would greenwash the three real
    ones. ``refused`` counts refusals, which are evidence of a boundary,
    not of coverage.
    """

    explicit: int = 0
    inferred: int = 0
    implicit: int = 0
    refused: int = 0


def count_bases(events: Iterable[LedgerEvent]) -> MandateCounts:
    """Count mandate bases over ledger events, separately per basis."""
    explicit = inferred = implicit = refused = 0
    for event in events:
        if event.event_type is EventType.MANDATE_REFUSED:
            refused += 1
        elif event.event_type is EventType.MANDATE_GRANTED:
            basis = event.payload.get("basis")
            if basis == MandateBasis.EXPLICIT.value:
                explicit += 1
            elif basis == MandateBasis.INFERRED.value:
                inferred += 1
            elif basis == MandateBasis.IMPLICIT.value:
                implicit += 1
            # No recognised basis: not evidence under v1, counted nowhere.
    return MandateCounts(
        explicit=explicit, inferred=inferred, implicit=implicit, refused=refused
    )


def head_mandate_summary(events: Iterable[LedgerEvent]) -> str | None:
    """A one-line summary of the first granted mandate, for ``paa ledger``.

    ``None`` when the lineage carries no grant — a lineage without a
    mandate is reported as such by the caller, never summarised as one.
    Reads the v1 payload's fields with tolerant defaults: this is a display
    of a record written by this or a newer binary, and a missing field must
    degrade the line, not crash the command.
    """
    for event in events:
        if event.event_type is not EventType.MANDATE_GRANTED:
            continue
        payload = event.payload
        asserted = payload.get("requester_asserted")
        if not isinstance(asserted, dict):
            asserted = {}
        scope = payload.get("scope")
        if not isinstance(scope, dict):
            scope = {}
        workspace = scope.get("workspace") or "-"
        return (
            f"basis={payload.get('basis', '?')} "
            f"requester={asserted.get('kind', '?')}:{asserted.get('id', '?')} "
            f"policy={payload.get('policy_label', '?')} workspace={workspace}"
        )
    return None
