"""Event-sourced ledger: the runtime's only source of truth.

Everything else — Redis queues, in-process caches, the filesystem — is a
derived view that can be rebuilt by replaying these events.
"""

from __future__ import annotations

from paa.ledger.events import (
    GENESIS_HASH,
    LedgerEvent,
    canonical_json,
    compute_event_digest,
    compute_idempotency_key,
)
from paa.ledger.replay import TaskPhase, TaskProjection, apply_event, project, replay
from paa.ledger.store import CorrelationHead, LedgerStore

__all__ = [
    "GENESIS_HASH",
    "CorrelationHead",
    "LedgerEvent",
    "LedgerStore",
    "TaskPhase",
    "TaskProjection",
    "apply_event",
    "canonical_json",
    "compute_event_digest",
    "compute_idempotency_key",
    "project",
    "replay",
]
