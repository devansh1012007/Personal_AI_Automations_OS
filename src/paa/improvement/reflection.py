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

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.types import EventType
from paa.memory.world_model import BeliefDocument, VaultConflictError, WorldModel
from paa.models.base import redact

if TYPE_CHECKING:
    from paa.ledger.store import LedgerStore

__all__ = [
    "REFLECTION_INTERVAL_DAYS",
    "REFLECTION_LAST_RUN_FILENAME",
    "DomainFriction",
    "ReflectionEngine",
    "ReflectionReport",
    "format_reflection_health",
    "operational_friction",
    "read_reflection_last_run",
    "reflection_health",
    "reflection_last_run_age_days",
    "reflection_last_run_path",
    "write_reflection_last_run",
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


# ---------------------------------------------------------------------------
# Last-run tracking (PLAN:integration-fix:P013). File-based on purpose: no
# REFLECTION_* EventType exists, and inventing one for this SURFACE task would
# drag in core/types.py, ledger/replay.py and the replay/TERMINAL_EVENTS
# checklist. The marker is a small JSON file under PAA_HOME holding
# {"last_run_at": <iso>}; it is bookkeeping, not truth — losing it only costs
# one early (idempotent-gated) re-run, and a corrupt one degrades to
# unknown/due the same way WorldModel._load_hashes degrades its write record.
# ---------------------------------------------------------------------------

#: Weekly cadence: a last run strictly older than this is stale and due.
#: Exactly 7 days is not stale (the T14 boundary pair); 7 days + 1s is.
REFLECTION_INTERVAL_DAYS = 7.0

#: Marker filename under PAA_HOME (Settings.home, not the vault: the vault is
#: human-editable markdown, this is machine bookkeeping).
REFLECTION_LAST_RUN_FILENAME = "reflection_last_run.json"


def reflection_last_run_path(home: Path | str) -> Path:
    """The last-run marker path for a runtime home."""
    return Path(home) / REFLECTION_LAST_RUN_FILENAME


def read_reflection_last_run(home: Path | str) -> datetime | None:
    """The last completed reflection run, or ``None`` when unknown.

    ``None`` covers a missing marker (first boot — due immediately), an
    unreadable/corrupt file, and a wrong-shaped payload. Every degraded case
    behaves identically (due, never a crash, never "fresh"), so a damaged
    marker costs at most one extra gated re-run.
    """
    from paa.storage.relational.database import from_iso

    path = reflection_last_run_path(home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # ValueError covers both JSONDecodeError and UnicodeDecodeError.
        log.warning("reflection.last_run_unreadable", error=redact(str(exc)))
        return None
    if not isinstance(data, dict) or not isinstance(data.get("last_run_at"), str):
        log.warning("reflection.last_run_unreadable", error="not a last_run_at object")
        return None
    try:
        return from_iso(data["last_run_at"])
    except (TypeError, ValueError) as exc:
        log.warning("reflection.last_run_unreadable", error=redact(str(exc)))
        return None


def write_reflection_last_run(home: Path | str, when: datetime) -> Path:
    """Record a completed run, atomically (temp file + os.replace).

    Same idiom as WorldModel._atomic_write: a crash mid-write leaves the old
    marker intact (trailing re-run) rather than a truncated one (false fresh).
    Naive ``when`` is treated as UTC like every other stored timestamp.
    """
    from paa.storage.relational.database import to_iso

    path = reflection_last_run_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps({"last_run_at": to_iso(when)}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def reflection_last_run_age_days(home: Path | str, *, now: datetime | None = None) -> float | None:
    """Days since the last run, or ``None`` when unknown.

    A negative result means the marker is in the future (clock skew); it is
    returned raw and treated as fresh by :func:`reflection_health` — the
    marker claims a run happened, so the loop waits rather than re-running.
    """
    last = read_reflection_last_run(home)
    if last is None:
        return None
    if now is None:
        from paa.storage.relational.database import utc_now

        now = utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - last).total_seconds() / 86400.0


def reflection_health(
    home: Path | str, *, enabled: bool, now: datetime | None = None
) -> dict[str, Any]:
    """The /doctor health view of the weekly reflection.

    ``due`` is gated on ``enabled`` (a disabled loop never fires); ``stale``
    describes the data alone (known age strictly over 7 days). Unknown (no
    marker) is due but not stale: unknown is not a claim of staleness.
    """
    last = read_reflection_last_run(home)
    age: float | None = None
    if last is not None:
        if now is None:
            from paa.storage.relational.database import utc_now

            now = utc_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        age = (now - last).total_seconds() / 86400.0
    stale = age is not None and age > REFLECTION_INTERVAL_DAYS
    due = bool(enabled and (age is None or age > REFLECTION_INTERVAL_DAYS))
    return {
        "enabled": enabled,
        "last_run_at": last,
        "last_run_age_days": age,
        "due": due,
        "stale": stale,
    }


def format_reflection_health(health: Mapping[str, Any]) -> str:
    """The one-string doctor value for a :func:`reflection_health` dict.

    The CLI doctor and the TUI mirror render this exact rule, so the two
    surfaces cannot disagree about the same home.
    """
    if not health.get("enabled"):
        return "disabled"
    age = health.get("last_run_age_days")
    if age is None:
        return "unknown (due)"
    if health.get("stale"):
        return f"{float(age):.1f}d (stale)"
    return f"{float(age):.1f}d"


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
