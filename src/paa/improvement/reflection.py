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
from typing import TYPE_CHECKING

import structlog

from paa.core.types import EventType

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
            report.rules_written.append(rule)
            if self._vault is not None:
                self.apply_to_playbook(rule)
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

        Same marker discipline as the world model: only the runtime's own
        block is touched, so a user's hand-written playbook notes are never
        overwritten (RFC §9).
        """
        if self._vault is None:
            return
        from paa.memory.world_model import WorldModel  # for its marker parser

        self._vault.mkdir(parents=True, exist_ok=True)
        path = self._vault / filename
        # Read the current managed block (if any) and append to it, so successive
        # weekly runs accumulate anti-patterns rather than overwriting last
        # week's. WorldModel owns the marker-parsing regex; reuse it.
        wm = WorldModel(self._vault)
        existing_managed = ""
        if path.exists():
            existing_managed = (
                wm._extract_block(path.read_text(encoding="utf-8"), "reflections") or ""
            )
        combined = (existing_managed + "\n" + rule).strip()
        _write_managed_block(path, "reflections", combined, title="Playbooks")
        log.info("reflection.playbook_updated", path=str(path))


# ---------------------------------------------------------------------------


def _domain_from_request(payload: dict) -> str:
    request = payload.get("request", payload)
    if isinstance(request, dict):
        if request.get("domain"):
            return str(request["domain"])
        goal = str(request.get("goal", "")).lower()
        for kw in ("deploy", "docker", "refactor", "test", "database", "auth", "api"):
            if kw in goal:
                return kw
    return "unknown"


def _write_managed_block(path: Path, section: str, content: str, *, title: str) -> None:
    """Marker-fenced atomic write, mirroring WorldModel but for any vault file."""
    import os
    import re
    import uuid

    begin = f"<!-- paa:managed:BEGIN {section} -->"
    end = f"<!-- paa:managed:END {section} -->"
    managed = f"{begin}\n{content.strip()}\n{end}"

    if path.exists():
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
        new_text = pattern.sub(managed, text, count=1) if pattern.search(text) else (
            text + ("" if text.endswith("\n") else "\n") + "\n" + managed + "\n"
        )
    else:
        header = f"# {title}\n\n_Managed block below is maintained by the runtime._\n\n"
        new_text = f"{header}{managed}\n"

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
