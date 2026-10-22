"""Execution-run telemetry. RFC §10.2.

One row per unit of contained work in ``hot_serving_execution_runs``, keyed by
``trace_id`` so a run joins directly to the span tree in
:mod:`paa.observability.tracing` and, through ``correlation_id``, to the ledger
lineage that caused it.

This table is *derived* state, not truth. The ledger is the only source of
truth (see :mod:`paa.ledger.store`); these rows are a queryable projection that
exists so the self-improvement optimiser can ask "how does this skill actually
perform?" without folding the entire event log. Losing this table costs
history, not correctness.

:meth:`ExecutionRunRepository.skill_stats` is the interface the optimiser
consumes. Its correction rate joins against real ``USER_CORRECTION`` events in
the ledger rather than reading a column here, because a correction is something
the *user* did to a lineage — it is not knowledge the run row ever had, and
duplicating it into one would create a second answer that could drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from paa.core.errors import PaaError
from paa.core.types import (
    ComplexityModality,
    CorrelationId,
    PermissionMode,
    SessionId,
    as_text,
    as_uuid,
)
from paa.storage.relational.base import RelationalBackend
from paa.storage.relational.database import dumps, from_iso, loads, to_iso, utc_now

__all__ = ["ExecutionRun", "ExecutionRunRepository", "InvalidTraceIdError", "SkillStats"]


class InvalidTraceIdError(PaaError):
    """A caller-supplied trace id is not a UUID.

    A trace id is a UUID, and a UUID has two spellings — the hyphenated
    one and the 32-character hex one — but it has no third. A value that
    is neither is refused at the write, because the column is a ``UUID``
    on one substrate and free text on the other: a value that is not a
    UUID would be stored by one and refused by the other, and the join
    between a run row and the span that owns it would match on one and
    not on the other.

    It is a :class:`~paa.core.errors.PaaError` rather than a bare
    ``ValueError`` because it is a failure of the *runtime's* contract
    with its caller, and the runtime's contract is expressed in
    ``PaaError``\\ s: a caller that catches ``PaaError`` at the edge of
    the system should not have to know that this one failure arrives as
    a different kind of exception.
    """

log = structlog.get_logger(__name__)

#: Exactly the columns declared in schema_sqlite.sql. Listed once so a typo is
#: a single-site fix and an added column is an obvious diff.
_COLUMNS = """
    trace_id, correlation_id, session_id, span_parent_id, agent_role, skill_name,
    modality, permission_mode, started_at, ended_at, duration_ms, exit_code,
    tokens_consumed, peak_rss_mb, model_used, escalated, telemetry, error_detail
"""

_INSERT_SQL = f"INSERT INTO hot_serving_execution_runs ({_COLUMNS}) VALUES ({','.join('?' * 18)})"


@dataclass(frozen=True, slots=True)
class ExecutionRun:
    """One row of ``hot_serving_execution_runs``."""

    trace_id: str
    correlation_id: uuid.UUID
    agent_role: str
    modality: str
    permission_mode: str
    started_at: datetime
    session_id: uuid.UUID | None = None
    span_parent_id: str | None = None
    skill_name: str | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    tokens_consumed: int = 0
    peak_rss_mb: float | None = None
    model_used: str | None = None
    escalated: bool = False
    telemetry: dict[str, Any] | None = None
    error_detail: str | None = None

    @property
    def is_finished(self) -> bool:
        return self.ended_at is not None

    @property
    def status(self) -> str:
        """``OK`` / ``ERROR`` / ``IN_FLIGHT``.

        An unfinished run is explicitly *not* a failure. Conflating the two is
        how a crashed-mid-run process makes a skill's success rate look
        catastrophic on the next boot, when in fact the run simply never
        reported — which is what the recovery sweep exists to resolve.
        """
        if self.ended_at is None:
            return "IN_FLIGHT"
        return "OK" if self.exit_code == 0 else "ERROR"

    def to_trace_json(self) -> dict[str, Any]:
        """The RFC §10.2 trace record.

        Grouped rather than flat — identity, timing, outcome, model, resources —
        because this is read by a human debugging a run and by the optimiser
        aggregating many, and both do better with the model-tier facts
        (``escalated``, ``model_used``, ``tokens_consumed``) collected in one
        place. ``escalated`` is a real boolean here even though SQLite stores it
        as ``0``/``1``.
        """
        return {
            "trace_id": self.trace_id,
            "correlation_id": str(self.correlation_id),
            "session_id": str(self.session_id) if self.session_id else None,
            "parent_span_id": self.span_parent_id,
            "agent_role": self.agent_role,
            "skill_name": self.skill_name,
            "modality": self.modality,
            "permission_mode": self.permission_mode,
            "started_at": to_iso(self.started_at),
            "ended_at": to_iso(self.ended_at) if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "exit_code": self.exit_code,
            "model": {
                "model_used": self.model_used,
                "escalated": self.escalated,
                "tokens_consumed": self.tokens_consumed,
            },
            "resources": {"peak_rss_mb": self.peak_rss_mb},
            "telemetry": self.telemetry or {},
            "error": self.error_detail,
        }


@dataclass(frozen=True, slots=True)
class SkillStats:
    """Aggregate performance of one skill. Consumed by the optimiser."""

    skill_name: str
    sample_count: int
    """Finished runs only. Unfinished ones are excluded rather than counted as
    failures — see :attr:`ExecutionRun.status`."""

    success_rate: float
    mean_latency_ms: float
    correction_rate: float
    """Share of this skill's correlations that a human later corrected. The
    optimiser's strongest quality signal: an exit code of 0 means the code ran,
    not that it did the right thing."""

    escalation_rate: float
    mean_tokens: float

    @property
    def is_significant(self) -> bool:
        """Whether the sample is large enough to act on.

        The optimiser must not rewrite a skill on the strength of two runs. Five
        is a low bar and deliberately so — this is a single-user runtime where
        genuinely rare skills exist — but it stops a single unlucky failure from
        reading as a 0% success rate.
        """
        return self.sample_count >= 5

    def to_payload(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "sample_count": self.sample_count,
            "success_rate": round(self.success_rate, 4),
            "mean_latency_ms": round(self.mean_latency_ms, 3),
            "correction_rate": round(self.correction_rate, 4),
            "escalation_rate": round(self.escalation_rate, 4),
            "mean_tokens": round(self.mean_tokens, 2),
            "is_significant": self.is_significant,
        }


def _no_sample_stats(skill_name: str) -> SkillStats:
    """The stats of a skill with no finished runs to speak of.

    Zeroes rather than an error, and a ``sample_count`` of zero is exactly
    what :attr:`SkillStats.is_significant` reads: the optimiser must not
    rewrite a skill on the strength of no evidence at all. An unfinished run
    is excluded rather than counted as a failure — see
    :attr:`ExecutionRun.status` — so "no evidence" is a state the aggregate
    has to be able to express.
    """
    return SkillStats(
        skill_name=skill_name,
        sample_count=0,
        success_rate=0.0,
        mean_latency_ms=0.0,
        correction_rate=0.0,
        escalation_rate=0.0,
        mean_tokens=0.0,
    )


class ExecutionRunRepository:
    """Reads and writes ``hot_serving_execution_runs``."""

    def __init__(self, db: RelationalBackend) -> None:
        self._db = db

    # -- writes ------------------------------------------------------------

    async def start_run(
        self,
        *,
        correlation_id: CorrelationId | uuid.UUID,
        agent_role: str,
        modality: ComplexityModality | str = ComplexityModality.STANDARD,
        permission_mode: PermissionMode | str = PermissionMode.ASK,
        session_id: SessionId | uuid.UUID | None = None,
        span_parent_id: str | None = None,
        skill_name: str | None = None,
        trace_id: str | None = None,
        model_used: str | None = None,
        started_at: datetime | None = None,
    ) -> str:
        """Open a run and return its ``trace_id``.

        The row is written *now*, with ``ended_at`` null, rather than being
        buffered until the run completes. A process killed mid-run therefore
        leaves evidence that the run started — which is precisely the row the
        recovery sweep needs, and precisely the row an in-memory buffer would
        have lost.

        Accepts a caller-supplied ``trace_id`` so a run can adopt the id of an
        already-open :class:`~paa.observability.tracing.Span` and the two views
        line up. A supplied id is **normalised** to the canonical hyphenated
        form, because the two substrates store a trace id differently
        otherwise: SQLite stores the text it was given, and PostgreSQL's
        ``UUID`` column normalises it to the hyphenated form. A caller that
        passed ``uuid4().hex`` would write the very form the schema
        version 4 migration exists to repair, and the join between a run row
        and its span would match on one substrate and not the other.

        :raises InvalidTraceIdError: if a supplied ``trace_id`` is not a
            UUID at all. The refusal happens at the write, where the
            mistake is, rather than at one substrate's insert, where the
            message would be about a column.
        """
        # The hyphenated form, not ``uuid4().hex``: a PostgreSQL ``UUID``
        # column normalises to the hyphenated form on the way in, so the
        # two substrates would store different bytes for the same id
        # otherwise — and a join between a run row and a span would match
        # on one substrate and not the other. A caller-supplied id is put
        # through the same normalisation, so the form the row carries does
        # not depend on who wrote it.
        resolved_trace_id = _canonical_trace_id(trace_id) if trace_id else str(uuid.uuid4())
        started = started_at or utc_now()

        await self._db.execute(
            _INSERT_SQL,
            (
                resolved_trace_id,
                str(correlation_id),
                str(session_id) if session_id else None,
                span_parent_id,
                agent_role,
                skill_name,
                _enum_value(modality),
                _enum_value(permission_mode),
                to_iso(started),
                None,  # ended_at
                None,  # duration_ms
                None,  # exit_code
                0,  # tokens_consumed
                None,  # peak_rss_mb
                model_used,
                0,  # escalated
                dumps({}),
                None,  # error_detail
            ),
        )
        log.debug(
            "runs.started",
            trace_id=resolved_trace_id,
            correlation_id=str(correlation_id),
            agent_role=agent_role,
            skill_name=skill_name,
        )
        return resolved_trace_id

    async def finish_run(
        self,
        trace_id: str,
        *,
        exit_code: int | None,
        telemetry: dict[str, Any] | None = None,
        tokens_consumed: int | None = None,
        peak_rss_mb: float | None = None,
        model_used: str | None = None,
        escalated: bool | None = None,
        error_detail: str | None = None,
        ended_at: datetime | None = None,
    ) -> ExecutionRun | None:
        """Close a run. Returns the completed row, or ``None`` if unknown.

        ``duration_ms`` is computed here from the stored ``started_at`` rather
        than taken from the caller. The caller's stopwatch and the row's start
        time can disagree — a retry, a resumed run, a clock the caller measured
        before the insert — and only one of them is what the table says.

        Every optional parameter left as ``None`` preserves the existing value.
        ``COALESCE(?, column)`` in the UPDATE is what makes a partial finish
        (say, an exit code without token counts) non-destructive.

        The two exceptions are the ones a *finish* is for: ``ended_at``,
        which defaults to now — a finish that did not end the run would
        leave it open forever, and ``IN_FLIGHT`` forever with it — and
        ``duration_ms``, which is computed rather than supplied.

        ``exit_code`` is preserved like the rest, and that matters because
        the status is derived from it: a caller that could not learn the
        exit code must not turn a recorded success into a failure by
        omitting the field. An omission is not a value.
        """
        existing = await self.get_run(trace_id)
        if existing is None:
            log.warning("runs.finish_unknown_trace", trace_id=trace_id)
            return None

        ended = ended_at or utc_now()
        duration_ms = max(0, int((ended - existing.started_at).total_seconds() * 1000))

        await self._db.execute(
            """
            UPDATE hot_serving_execution_runs SET
                ended_at        = ?,
                duration_ms     = ?,
                exit_code       = COALESCE(?, exit_code),
                tokens_consumed = COALESCE(?, tokens_consumed),
                peak_rss_mb     = COALESCE(?, peak_rss_mb),
                model_used      = COALESCE(?, model_used),
                escalated       = COALESCE(?, escalated),
                telemetry       = COALESCE(?, telemetry),
                error_detail    = COALESCE(?, error_detail)
            WHERE trace_id = ?
            """,
            (
                to_iso(ended),
                duration_ms,
                exit_code,
                None if tokens_consumed is None else int(tokens_consumed),
                None if peak_rss_mb is None else float(peak_rss_mb),
                model_used,
                None if escalated is None else int(bool(escalated)),
                None if telemetry is None else dumps(telemetry),
                error_detail,
                trace_id,
            ),
        )
        log.debug("runs.finished", trace_id=trace_id, exit_code=exit_code, ms=duration_ms)
        return await self.get_run(trace_id)

    # -- reads -------------------------------------------------------------

    async def get_run(self, trace_id: str) -> ExecutionRun | None:
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM hot_serving_execution_runs WHERE trace_id = ?",
            (trace_id,),
        )
        return _row_to_run(row) if row else None

    async def query_runs(
        self,
        *,
        correlation_id: CorrelationId | uuid.UUID | None = None,
        session_id: SessionId | uuid.UUID | None = None,
        skill_name: str | None = None,
        agent_role: str | None = None,
        since: datetime | None = None,
        only_finished: bool = False,
        limit: int = 200,
    ) -> list[ExecutionRun]:
        """Filtered scan, newest first.

        Filters compose as AND. Every parameter left at ``None`` is omitted from
        the WHERE clause entirely rather than becoming ``column IS NULL``, which
        would be a different (and almost never intended) question.
        """
        clauses: list[str] = []
        params: list[Any] = []

        for column, value in (
            ("correlation_id", str(correlation_id) if correlation_id else None),
            ("session_id", str(session_id) if session_id else None),
            ("skill_name", skill_name),
            ("agent_role", agent_role),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)

        if since is not None:
            clauses.append("started_at >= ?")
            params.append(to_iso(since))
        if only_finished:
            clauses.append("ended_at IS NOT NULL")

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM hot_serving_execution_runs{where} "
            "ORDER BY started_at DESC LIMIT ?",
            params,
        )
        return [_row_to_run(r) for r in rows]

    async def skill_stats(self, skill_name: str) -> SkillStats:
        """Aggregates for one skill.

        Two queries rather than one join. The correction count is a
        ``COUNT(DISTINCT correlation_id)`` over the ledger, and folding that
        into the runs aggregate would multiply the run-side sums by the number
        of matching ledger events — the classic fan-out bug, which produces
        numbers that look plausible and are wrong by an integer factor.
        """
        row = await self._db.fetch_one(
            """
            SELECT
                COUNT(*)                                        AS finished,
                SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END)  AS successes,
                SUM(CASE WHEN escalated = 1 THEN 1 ELSE 0 END)  AS escalations,
                AVG(duration_ms)                                AS mean_ms,
                AVG(tokens_consumed)                            AS mean_tokens,
                COUNT(DISTINCT correlation_id)                  AS correlations
            FROM hot_serving_execution_runs
            WHERE skill_name = ? AND ended_at IS NOT NULL
            """,
            (skill_name,),
        )

        # A missing row is not "no runs" — an aggregate with no ``GROUP BY``
        # always returns exactly one row — so it means the backend answered
        # nothing at all. The zeroed stats are the honest reading either way:
        # the optimiser's rule is that an empty sample is not a failure, and
        # a skill with no evidence is a skill it must not rewrite.
        if row is None:
            return _no_sample_stats(skill_name)

        finished = int(row["finished"] or 0)
        if finished == 0:
            return _no_sample_stats(skill_name)

        correlations = int(row["correlations"] or 0)
        corrected = int(
            await self._db.fetch_value(
                """
                SELECT COUNT(DISTINCT l.correlation_id)
                FROM system_state_ledger AS l
                WHERE l.event_type = 'USER_CORRECTION'
                  AND l.correlation_id IN (
                      SELECT DISTINCT correlation_id
                      FROM hot_serving_execution_runs
                      WHERE skill_name = ? AND ended_at IS NOT NULL
                  )
                """,
                (skill_name,),
            )
            or 0
        )

        return SkillStats(
            skill_name=skill_name,
            sample_count=finished,
            success_rate=int(row["successes"] or 0) / finished,
            mean_latency_ms=float(row["mean_ms"] or 0.0),
            correction_rate=(corrected / correlations) if correlations else 0.0,
            escalation_rate=int(row["escalations"] or 0) / finished,
            mean_tokens=float(row["mean_tokens"] or 0.0),
        )

    async def count(self) -> int:
        return int(
            await self._db.fetch_value("SELECT COUNT(*) FROM hot_serving_execution_runs") or 0
        )

    async def purge_older_than(self, cutoff: datetime) -> int:
        """Delete finished runs started before ``cutoff``.

        Honours ``ObservabilitySettings.trace_retention_days``. Unfinished runs
        are never purged regardless of age — an ancient in-flight row is
        evidence of an unresolved crash, which is the last thing a retention
        sweep should tidy away.
        """
        return await self._db.execute(
            "DELETE FROM hot_serving_execution_runs "
            "WHERE started_at < ? AND ended_at IS NOT NULL",
            (to_iso(cutoff),),
        )


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _canonical_trace_id(trace_id: str) -> str:
    """The canonical hyphenated form of a trace id.

    A trace id is a UUID, and a UUID has two spellings: the hyphenated one
    and the 32-character hex one. Both substrates store the hyphenated
    form — SQLite because the repository writes it that way, PostgreSQL
    because its ``UUID`` column normalises it — so a caller-supplied id is
    put through the same normalisation here rather than being trusted to
    arrive in that form.

    :raises InvalidTraceIdError: if the id is not a UUID at all. The
        column is a UUID on one substrate and free text on the other, so a
        value that is not a UUID would be stored by one and refused by the
        other; saying so at the write is better than a substrate-specific
        failure later.
    """
    try:
        return str(uuid.UUID(trace_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidTraceIdError(
            f"a trace id must be a UUID, and {trace_id!r} is not one",
            trace_id=trace_id,
        ) from exc


def _row_to_run(row: Any) -> ExecutionRun:
    return ExecutionRun(
        trace_id=as_text(row["trace_id"]),
        correlation_id=as_uuid(row["correlation_id"]),
        session_id=as_uuid(row["session_id"]) if row["session_id"] else None,
        span_parent_id=(
            as_text(row["span_parent_id"]) if row["span_parent_id"] is not None else None
        ),
        agent_role=row["agent_role"],
        skill_name=row["skill_name"],
        modality=row["modality"],
        permission_mode=row["permission_mode"],
        started_at=from_iso(row["started_at"]),
        ended_at=from_iso(row["ended_at"]) if row["ended_at"] else None,
        duration_ms=row["duration_ms"],
        exit_code=row["exit_code"],
        tokens_consumed=int(row["tokens_consumed"] or 0),
        peak_rss_mb=row["peak_rss_mb"],
        model_used=row["model_used"],
        escalated=bool(row["escalated"]),
        telemetry=loads(row["telemetry"], {}),
        error_detail=row["error_detail"],
    )
