"""A snapshot of everything the dashboard needs to render right now.

The dispatcher is pure: it takes a :class:`Snapshot` and returns a
:class:`CommandResult`. The runner rebuilds a snapshot on each poll tick and
asks the dispatcher again, so an in-flight command never touches stale state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from paa.core.types import ComplexityModality, CorrelationId
from paa.ledger.replay import TaskPhase

__all__ = [
    "CorrelationRow",
    "Snapshot",
    "parse_correlation_id",
]


@dataclass(frozen=True)
class CorrelationRow:
    """A lightweight projection summary for one open task lineage."""

    correlation_id: CorrelationId
    phase: TaskPhase
    modality: ComplexityModality
    attempts: int
    tokens_consumed: int
    created_at: str | None = None
    goal_preview: str | None = None


@dataclass
class Snapshot:
    """Point-in-time view of the runtime, rebuilt every poll tick."""

    rows: list[CorrelationRow] = field(default_factory=list)
    runtime_info: dict[str, Any] = field(default_factory=dict)

    def row_for(self, correlation_id: CorrelationId | str) -> CorrelationRow | None:
        cid_str = str(correlation_id)
        for row in self.rows:
            if str(row.correlation_id) == cid_str:
                return row
        return None


def parse_correlation_id(value: str) -> CorrelationId:
    """Parse a correlation id string, returning a typed CorrelationId."""
    try:
        return CorrelationId(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"invalid correlation id: {value!r}") from exc
