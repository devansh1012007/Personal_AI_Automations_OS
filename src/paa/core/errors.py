"""Exception hierarchy for the cognitive runtime.

Design rule: every exception carries enough structured context to be written
straight into a ledger event payload without the caller having to reconstruct
what went wrong. That is why each class takes explicit fields rather than a
formatted message.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BudgetExceededError",
    "ContextInsufficientError",
    "ContradictionError",
    "IdempotencyViolation",
    "LedgerError",
    "PaaError",
    "PermissionDeniedError",
    "PolicyViolation",
    "RecursionGuardError",
    "ReplayIntegrityError",
    "SandboxError",
    "SandboxTimeout",
    "SchemaValidationError",
    "SecurityScanError",
    "SkillContractError",
    "StorageError",
    "ValidationError",
]


class PaaError(Exception):
    """Base for every runtime error.

    ``details`` is merged into the ledger event payload verbatim, so keys should
    be JSON-serialisable and stable enough to query on later.
    """

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def __str__(self) -> str:
        if not self.details:
            return self.message
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
        return f"{self.message} ({rendered})"

    def to_payload(self) -> dict[str, Any]:
        """Structured form for ledger persistence."""
        return {"error_type": type(self).__name__, "message": self.message, **self.details}


# ---------------------------------------------------------------------------
# Ledger + recovery
# ---------------------------------------------------------------------------


class LedgerError(PaaError):
    """Base for append-only ledger failures."""


class IdempotencyViolation(LedgerError):
    """A duplicate event was suppressed by the idempotency constraint.

    This is frequently *benign* — it is exactly how at-least-once delivery is
    made effectively-once. Callers that retry should catch it and treat the
    existing event as authoritative.
    """

    def __init__(self, idempotency_key: str, existing_sequence_id: int | None = None) -> None:
        super().__init__(
            "duplicate ledger event suppressed",
            idempotency_key=idempotency_key,
            existing_sequence_id=existing_sequence_id,
        )
        self.idempotency_key = idempotency_key
        self.existing_sequence_id = existing_sequence_id


class ReplayIntegrityError(LedgerError):
    """Replay produced a state that disagrees with observed reality.

    Raised when a projected workspace checksum does not match what is actually
    on disk — the signal that a crash left the filesystem drifted from the log.
    """

    def __init__(
        self,
        correlation_id: str,
        expected_checksum: str | None,
        actual_checksum: str | None,
        path: str | None = None,
    ) -> None:
        super().__init__(
            "replayed state diverges from on-disk state",
            correlation_id=correlation_id,
            expected_checksum=expected_checksum,
            actual_checksum=actual_checksum,
            path=path,
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageError(PaaError):
    """A storage substrate refused or failed an operation."""

    def __init__(self, message: str, *, substrate: str, **details: Any) -> None:
        super().__init__(message, substrate=substrate, **details)
        self.substrate = substrate


# ---------------------------------------------------------------------------
# Budgets + recursion
# ---------------------------------------------------------------------------


class BudgetExceededError(PaaError):
    """A task exhausted an allocated budget (tokens, wall clock, or nodes)."""

    def __init__(self, budget_kind: str, limit: float, consumed: float, **details: Any) -> None:
        super().__init__(
            f"{budget_kind} budget exhausted",
            budget_kind=budget_kind,
            limit=limit,
            consumed=consumed,
            **details,
        )
        self.budget_kind = budget_kind
        self.limit = limit
        self.consumed = consumed


class RecursionGuardError(PaaError):
    """A delegation would breach the depth ceiling or close a dependency cycle.

    RFC §11 names this ``RecursionGuardException``; renamed to the Python
    ``*Error`` convention.
    """

    def __init__(
        self,
        reason: str,
        *,
        depth: int | None = None,
        ceiling: int | None = None,
        cycle: list[str] | None = None,
    ) -> None:
        super().__init__(reason, depth=depth, ceiling=ceiling, cycle=cycle)
        self.depth = depth
        self.ceiling = ceiling
        self.cycle = cycle or []


# ---------------------------------------------------------------------------
# Policy + security
# ---------------------------------------------------------------------------


class PolicyViolation(PaaError):
    """The policy agent refused an action."""

    def __init__(
        self,
        rule: str,
        *,
        similarity: float | None = None,
        threshold: float | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            f"policy rule refused the action: {rule}",
            rule=rule,
            similarity=similarity,
            threshold=threshold,
            **details,
        )
        self.rule = rule


class PermissionDeniedError(PolicyViolation):
    """A required permission is not granted by the active permission mode."""

    def __init__(self, missing: list[str], mode: str) -> None:
        PaaError.__init__(
            self,
            "required permissions are not granted in the active mode",
            missing=missing,
            mode=mode,
        )
        self.rule = "permission_grant"
        self.missing = missing
        self.mode = mode


class SecurityScanError(PolicyViolation):
    """Static analysis found a forbidden construct before execution."""

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        PaaError.__init__(
            self, "static security scan rejected the payload", finding_count=len(findings)
        )
        self.rule = "ast_security_scan"
        self.findings = findings


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


class SandboxError(PaaError):
    """Sandboxed execution failed."""

    def __init__(self, message: str, *, exit_code: int | None = None, **details: Any) -> None:
        super().__init__(message, exit_code=exit_code, **details)
        self.exit_code = exit_code


class SandboxTimeout(SandboxError):
    """Execution exceeded its wall-clock budget and was killed."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__("sandbox exceeded wall-clock budget", timeout_seconds=timeout_seconds)
        self.timeout_seconds = timeout_seconds


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationError(PaaError):
    """Deterministic validation rejected an artifact."""


class SchemaValidationError(ValidationError):
    """A payload did not conform to its declared schema."""

    def __init__(self, schema_name: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(
            "payload failed schema validation",
            schema_name=schema_name,
            error_count=len(errors),
        )
        self.schema_name = schema_name
        self.errors = errors


class SkillContractError(ValidationError):
    """A skill registration failed its contract checks."""


# ---------------------------------------------------------------------------
# Context + memory
# ---------------------------------------------------------------------------


class ContextInsufficientError(PaaError):
    """The gatherer could not fill enough required slots to proceed safely."""

    def __init__(self, density: float, vacant_slots: list[str]) -> None:
        super().__init__(
            "insufficient context density to proceed",
            density=density,
            vacant_slots=vacant_slots,
        )
        self.density = density
        self.vacant_slots = vacant_slots


class ContradictionError(PaaError):
    """Two facts conflict above the resolution threshold and need a human.

    Per RFC §4.2 the runtime never auto-resolves; both records are quarantined
    and the task is parked on ``AWAITING_HUMAN_ATTESTATION``.
    """

    def __init__(self, entity_id: str, predicate: str, conflict_score: float) -> None:
        super().__init__(
            "unresolved fact contradiction requires human attestation",
            entity_id=entity_id,
            predicate=predicate,
            conflict_score=conflict_score,
        )
        self.entity_id = entity_id
        self.predicate = predicate
        self.conflict_score = conflict_score
