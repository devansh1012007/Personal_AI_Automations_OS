"""The Weekly Reflection Engine — the runtime learning from its own friction.

RFC §3.1. Once a week it reads the ledger, finds task domains that cost the user
corrections and rollbacks, and distils an anti-pattern into the playbook so the
next similar task goes better.

SPEC DEVIATION (docs/adr/0016): the RFC's Operational Friction Score divides by
"Total Successful Commits". For a domain that has *only ever failed* that
denominator is zero — and that domain is precisely the one most in need of
reflection. Division by zero, or (worse) a silently skipped domain. We use
``max(successes, 1)``, so a domain with 4 failures and 0 successes scores its
full friction rather than crashing or vanishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.types import EventType
from paa.memory.world_model import BeliefDocument, VaultConflictError, WorldModel
from paa.models.base import redact

if TYPE_CHECKING:
    from paa.ledger.store import LedgerStore

__all__ = [
    "DomainFriction",
    "ReflectionEngine",
    "ReflectionReport",
    "operational_friction",
]

log = structlog.get_logger(__name__)

#: RFC §3.1 weights and threshold.
_ALPHA_CORRECTION = 1.5
_BETA_ROLLBACK = 3.0
_FRICTION_THRESHOLD = 0.40


def operational_friction(
    corrections: int,
    rollbacks: int,
    successes: int,
    *,
    alpha: float = _ALPHA_CORRECTION,
    beta: float = _BETA_ROLLBACK,
) -> float:
    """F_ops = (corrections*alpha + rollbacks*beta) / max(successes, 1).

    The ``max(successes, 1)`` is the fix (ADR-0016): a domain with zero
    successful commits returns its numerator as its score instead of dividing
    by zero, so the worst-performing domains are the *most* visible to
    reflection, not invisible.
    """
    if corrections < 0 or rollbacks < 0 or successes < 0:
        raise ValueError("counts must be non-negative")
    numerator = corrections * alpha + rollbacks * beta
    return round(numerator / max(successes, 1), 6)


@dataclass(slots=True)
class DomainFriction:
    """Friction accounting for one task domain over the window."""

    domain: str
    corrections: int = 0
    rollbacks: int = 0
    successes: int = 0

    @property
    def score(self) -> float:
        return operational_friction(self.corrections, self.rollbacks, self.successes)

    @property
    def is_high_friction(self) -> bool:
        return self.score >= _FRICTION_THRESHOLD


@dataclass(slots=True)
class ReflectionReport:
    """What one weekly pass found and did."""

    window_start: datetime
    window_end: datetime
    domains: list[DomainFriction] = field(default_factory=list)
    rules_written: list[str] = field(default_factory=list)

    @property
    def high_friction_domains(self) -> list[DomainFriction]:
        return sorted(
            (d for d in self.domains if d.is_high_friction),
            key=lambda d: d.score,
            reverse=True,
        )


class ReflectionEngine:
    """Analyses a time window of ledger history and updates the playbook."""

    def __init__(
        self,
        store: LedgerStore,
        *,
        vault_path: Path | str | None = None,
        summarizer: object | None = None,
    ) -> None:
        self._store = store
        self._vault = Path(vault_path) if vault_path else None
        self._summarizer = summarizer

    async def analyze_window(
        self, *, since: datetime, until: datetime | None = None
    ) -> ReflectionReport:
        """Group ledger events by task domain and compute friction per domain.

        The domain is taken from a task's request payload (its ``domain`` or a
        keyword of its goal); corrections, rollbacks and commits are counted
        from the events in the window.
        """
        from paa.storage.relational.database import utc_now

        until = until or utc_now()
        events = await self._store.events_since(
            since,
            event_types=[
                EventType.USER_CORRECTION,
                EventType.STATE_ROLLBACK_TRIGGERED,
                EventType.MUTATION_COMMITTED,
                EventType.TASK_REQUESTED,
            ],
        )

        # Map correlation -> domain from TASK_REQUESTED, then tally.
        domain_of: dict[str, str] = {}
        for ev in events:
            if ev.event_type is EventType.TASK_REQUESTED:
                domain_of[str(ev.correlation_id)] = _domain_from_request(ev.payload)

        tallies: dict[str, DomainFriction] = {}

        def bump(cid: str, field_name: str) -> None:
            domain = domain_of.get(cid, "unknown")
            df = tallies.setdefault(domain, DomainFriction(domain=domain))
            setattr(df, field_name, getattr(df, field_name) + 1)

        for ev in events:
            cid = str(ev.correlation_id)
            if ev.event_type is EventType.USER_CORRECTION:
                bump(cid, "corrections")
            elif ev.event_type is EventType.STATE_ROLLBACK_TRIGGERED:
                bump(cid, "rollbacks")
            elif ev.event_type is EventType.MUTATION_COMMITTED:
                bump(cid, "successes")

        report = ReflectionReport(
            window_start=since, window_end=until, domains=list(tallies.values())
        )
        log.info(
            "reflection.analyzed",
            domains=len(report.domains),
            high_friction=len(report.high_friction_domains),
        )
        return report

    async def run_weekly(self, *, now: datetime | None = None) -> ReflectionReport:
        """Analyse the last 7 days and write anti-patterns for hot domains."""
        from paa.storage.relational.database import utc_now

        until = now or utc_now()
        since = until - timedelta(days=7)
        report = await self.analyze_window(since=since, until=until)

        for df in report.high_friction_domains:
            rule = self.synthesize_rule(df)
            if self._vault is None:
                report.rules_written.append(rule)
                continue
            try:
                self.apply_to_playbook(rule)
            except VaultConflictError as exc:
                # R-W3-003: the fence exists to surface a human edit inside
                # the playbook block, not to abort the weekly learning loop.
                # Skip this domain's rule, keep the accumulated report, and
                # try the next domain. `rules_written` only counts rules
                # that actually landed, so a skipped rule is not listed.
                log.warning(
                    "reflection.playbook_conflict_skipped",
                    domain=df.domain,
                    path=str(exc.path),
                    error=redact(str(exc)),
                )
            else:
                report.rules_written.append(rule)
        return report

    def synthesize_rule(self, friction: DomainFriction) -> str:
        """Turn a high-friction domain into a markdown anti-pattern entry.

        Deterministic by default; an injected summarizer may produce richer
        prose, but the deterministic template means reflection works with no
        model available — the learning loop must not depend on one.
        """
        if self._summarizer is not None:
            try:
                return str(self._summarizer(friction))  # type: ignore[operator]
            except Exception as exc:
                log.warning("reflection.summarizer_failed", error=str(exc))

        return (
            f"### Anti-pattern: {friction.domain} (friction {friction.score:.2f})\n"
            f"- Observed {friction.corrections} correction(s) and "
            f"{friction.rollbacks} rollback(s) against "
            f"{friction.successes} clean commit(s) this week.\n"
            f"- Enforced strategy: treat '{friction.domain}' tasks as higher-risk — "
            f"prefer a human gate and a dry-run before mutating.\n"
        )

    def apply_to_playbook(self, rule: str, *, filename: str = "playbooks.md") -> None:
        """Append an anti-pattern under a managed marker, preserving human text.

        PLAN:integration-fix:P010 (ADR-0038): the fence write is delegated to
        :meth:`WorldModel.update_belief_state` for ``playbooks.md`` — one
        fence implementation owns the markers, conflict detection and the
        write record. The duplicate writer that used to live here had to
        agree on marker spelling byte-for-byte to stay compatible; deleting
        it kills that drift. A human edit inside the fence therefore raises
        ``VaultConflictError`` (naming the path) instead of being silently
        overwritten. ``BeliefDocument.PLAYBOOKS`` governs the file, so
        ``filename`` must name it — anything else is rejected rather than
        joined onto the vault (closed set, as for the CLI).
        """
        if self._vault is None:
            return
        if filename != BeliefDocument.PLAYBOOKS.value:
            raise ValueError(f"not a governed playbook filename: {filename!r}")
        wm = WorldModel(self._vault)
        # Read the current managed block (if any) and append to it, so successive
        # weekly runs accumulate anti-patterns rather than overwriting last
        # week's. WorldModel owns the marker-parsing regex; reuse it.
        existing_managed = wm.read_managed(BeliefDocument.PLAYBOOKS, "reflections")
        combined = (existing_managed + "\n" + rule).strip()
        wm.update_belief_state(
            BeliefDocument.PLAYBOOKS, combined, section="reflections", source="reflection"
        )
        log.info(
            "reflection.playbook_updated",
            path=str(wm.path_for(BeliefDocument.PLAYBOOKS)),
        )


# ---------------------------------------------------------------------------


def _domain_from_request(payload: dict[str, Any]) -> str:
    request = payload.get("request", payload)
    if isinstance(request, dict):
        if request.get("domain"):
            return str(request["domain"])
        goal = str(request.get("goal", "")).lower()
        for kw in ("deploy", "docker", "refactor", "test", "database", "auth", "api"):
            if kw in goal:
                return kw
    return "unknown"
