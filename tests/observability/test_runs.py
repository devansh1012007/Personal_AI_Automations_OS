"""The run record — one row per execution, and the ids it carries.

Why this is tested at all
-------------------------
The observability subsystem has no other coverage, and the run record is
where the two relational substrates disagree most concretely: a trace id
is a UUID, and the two substrates store a UUID differently. SQLite stores
the text it was given; PostgreSQL's ``UUID`` column normalises whatever it
is given to the hyphenated form. A trace id written as 32 hex characters
was therefore stored in two different forms by the two substrates, and the
model's ``trace_id: str`` field received a ``uuid.UUID`` object on the
server — a value ``json.dumps`` refuses.

The invariants
--------------
* A trace id is the *same string* on both substrates — the canonical
  hyphenated form.
* A parent span id is storable, and it is text on both substrates. A span
  id is 64 bits and is not a UUID, so a ``UUID`` column refuses the only
  value it is ever given.
* The trace id a caller is handed is the trace id that comes back.

Why the *conversion* is tested and not just the round trip
-----------------------------------------------------------
Every test above runs against the embedded substrate, where an id column
is TEXT and the row already hands back a ``str``. Deleting
``as_text(row["trace_id"])`` from the row → model conversion would not
fail one of them: the value SQLite produces is already the value the
model declares. The server substrate is the one that produces a
:class:`uuid.UUID` from a ``UUID`` column, and that is the shape the
coercion exists for.

So the conversion is tested against a row the embedded substrate cannot
produce — a row whose ids are real :class:`uuid.UUID` objects. That is
the shape the server hands back, and it is the shape that made the
one-hop graph read raise a pydantic ``ValidationError`` where a model
was declared.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.core.types import (
    ComplexityModality,
    PermissionMode,
    as_text,
    new_correlation_id,
    new_session_id,
)
from paa.observability.runs import ExecutionRun, ExecutionRunRepository, InvalidTraceIdError
from paa.observability.tracing import _new_span_id, _new_trace_id
from paa.storage.relational.database import Database


@pytest.fixture
async def runs(db: Database) -> ExecutionRunRepository:
    return ExecutionRunRepository(db)


#: Values that are neither a ``str`` nor a :class:`uuid.UUID`.
#:
#: Drawn from, not enumerated, so that the refusal is a property over
#: every shape a column can hand back rather than a list of three
#: examples. Every branch here produces a value that is *definitely* not
#: an identifier, so the property cannot pass by accident.
_NOT_AN_IDENTIFIER = st.one_of(
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(),
    st.none(),
    st.lists(st.text(max_size=8)),
    st.dictionaries(st.text(max_size=4), st.text(max_size=8)),
    st.binary(max_size=8),
    st.tuples(st.text(max_size=4)),
)


class TestIdentifiers:
    async def test_a_fresh_trace_id_is_the_canonical_form(self) -> None:
        """The hyphenated form, because that is what a UUID column stores.

        Written as 32 hex characters, the same id would be stored in two
        different forms by the two substrates.
        """
        trace_id = _new_trace_id()
        assert len(trace_id) == 36
        assert trace_id.count("-") == 4
        assert str(uuid.UUID(trace_id)) == trace_id

    async def test_a_span_id_is_not_a_uuid(self) -> None:
        """A span id is 64 bits, so it cannot be a UUID.

        This is why the column that holds a parent span id is text: a
        ``UUID`` column would refuse the only value it is ever given.
        """
        span_id = _new_span_id()
        assert len(span_id) == 16
        with pytest.raises(ValueError):
            uuid.UUID(span_id)

    async def test_the_trace_id_handed_back_is_the_one_stored(
        self, runs: ExecutionRunRepository
    ) -> None:
        cid = new_correlation_id()
        trace_id = await runs.start_run(
            correlation_id=cid, agent_role="planner", modality=ComplexityModality.STANDARD
        )

        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.trace_id == trace_id
        assert isinstance(run.trace_id, str)

    async def test_a_parent_span_id_round_trips(self, runs: ExecutionRunRepository) -> None:
        """The value the column is meant to hold is the value it holds."""
        cid = new_correlation_id()
        span_id = _new_span_id()
        trace_id = await runs.start_run(
            correlation_id=cid, agent_role="planner", span_parent_id=span_id
        )

        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.span_parent_id == span_id
        assert isinstance(run.span_parent_id, str)

    async def test_a_run_with_no_parent_has_none(self, runs: ExecutionRunRepository) -> None:
        cid = new_correlation_id()
        trace_id = await runs.start_run(correlation_id=cid, agent_role="planner")
        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.span_parent_id is None

    async def test_a_caller_supplied_trace_id_is_kept(self, runs: ExecutionRunRepository) -> None:
        """A run can adopt the id of an already-open span, and the two line up."""
        cid = new_correlation_id()
        trace_id = _new_trace_id()
        returned = await runs.start_run(
            correlation_id=cid, agent_role="planner", trace_id=trace_id
        )
        assert returned == trace_id
        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.trace_id == trace_id

    async def test_a_caller_supplied_trace_id_is_normalised(
        self, runs: ExecutionRunRepository
    ) -> None:
        """A supplied id is put in the canonical form, not stored as it came.

        The two substrates store a trace id differently: SQLite stores the
        text it was given, and PostgreSQL's ``UUID`` column normalises it
        to the hyphenated form. A caller that passed ``uuid4().hex`` would
        therefore write the very form the schema version 4 migration
        exists to repair, and the join between a run row and its span
        would match on one substrate and not the other. The repository
        normalises instead, so the form the row carries does not depend on
        who wrote it.
        """
        cid = new_correlation_id()
        unhyphenated = uuid.uuid4().hex
        returned = await runs.start_run(
            correlation_id=cid, agent_role="planner", trace_id=unhyphenated
        )
        assert returned == str(uuid.UUID(unhyphenated))
        run = await runs.get_run(returned)
        assert run is not None
        assert run.trace_id == returned
        assert "-" in run.trace_id

    async def test_a_trace_id_that_is_not_a_uuid_is_refused(
        self, runs: ExecutionRunRepository
    ) -> None:
        """A trace id is a UUID, and a value that is not one is refused.

        The column is a ``UUID`` on PostgreSQL and free text on SQLite, so
        a value that is not a UUID would be stored by one and refused by
        the other — and the join between a run row and the span that owns
        it would match on one substrate and not on the other. Saying so at
        the write is better than a substrate-specific failure later.

        The refusal is a ``PaaError``: it is a failure of the runtime's
        contract with its caller, and the runtime's contract is expressed
        in ``PaaError``\\ s. A bare ``ValueError`` would be the one
        failure a caller catching ``PaaError`` at the edge of the system
        would not catch.
        """
        cid = new_correlation_id()
        with pytest.raises(InvalidTraceIdError):
            await runs.start_run(
                correlation_id=cid, agent_role="planner", trace_id="not-a-uuid"
            )

    async def test_a_run_serialises_to_json(self, runs: ExecutionRunRepository) -> None:
        """The run record ends up in a JSON document, so it has to be JSON.

        A ``uuid.UUID`` where a ``str`` was declared is not serialisable,
        and the failure surfaces wherever the telemetry is written — long
        after the run.
        """
        cid = new_correlation_id()
        trace_id = await runs.start_run(
            correlation_id=cid, agent_role="planner", span_parent_id=_new_span_id()
        )
        run = await runs.get_run(trace_id)
        assert run is not None
        assert json.dumps(run.to_trace_json())

    async def test_the_correlation_id_is_a_uuid(self, runs: ExecutionRunRepository) -> None:
        cid = new_correlation_id()
        trace_id = await runs.start_run(correlation_id=cid, agent_role="planner")
        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.correlation_id == cid
        assert isinstance(run.correlation_id, uuid.UUID)

    async def test_a_session_id_is_kept(self, runs: ExecutionRunRepository) -> None:
        cid = new_correlation_id()
        sid = new_session_id()
        trace_id = await runs.start_run(
            correlation_id=cid, agent_role="planner", session_id=sid
        )
        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.session_id == sid


class TestTheIdCoercion:
    """The helper that makes one column mean one thing on both substrates.

    Every id this repository reads arrives in one of two shapes: text on
    the embedded substrate, a :class:`uuid.UUID` on the server one. The
    model declares the id as a string, so the *reader* coerces — and the
    coercion has to be strict, because a silent pass-through is how a
    value that arrived as something else entirely reaches a model that
    declares it as text.
    """

    def test_text_is_returned_unchanged(self) -> None:
        assert as_text("already text") == "already text"

    def test_a_uuid_becomes_its_canonical_text(self) -> None:
        """The hyphenated lowercase form, which is what SQLite stores."""
        value = uuid.uuid4()
        assert as_text(value) == str(value)
        assert as_text(value).count("-") == 4

    def test_a_value_of_an_unknown_type_is_refused(self) -> None:
        """Loudly, rather than being passed through.

        ``as_text`` used to return whatever it was handed, so ``as_text(5)``
        produced the integer ``5`` under a signature that promised a
        string — and the mistake surfaced later, in a model that
        declares the field as text.
        """
        with pytest.raises(TypeError):
            as_text(5)  # type: ignore[arg-type]

    def test_a_null_is_refused_rather_than_stringified(self) -> None:
        """A NULL must never become the string ``"None"`` or ``""``.

        A nullable column is guarded at the call site instead — the same
        pattern ``as_uuid`` is used with — and the guard is what keeps
        the NULL a NULL.
        """
        with pytest.raises(TypeError):
            as_text(None)  # type: ignore[arg-type]

    def test_a_list_is_refused(self) -> None:
        """A list is not an identifier on either substrate.

        The refusal is not about the type being exotic — it is about the
        value having no canonical text form. A list handed to a field
        declared as text would be stringified by ``repr`` somewhere
        downstream, and a ``repr`` is not an identifier: it is a
        debugging artefact that happens to be printable.
        """
        with pytest.raises(TypeError):
            as_text(["a", "b"])  # type: ignore[arg-type]

    @given(value=_NOT_AN_IDENTIFIER)
    @hyp_settings(max_examples=200, deadline=None)
    def test_every_value_that_is_not_an_identifier_is_refused(self, value: Any) -> None:
        """The refusal is a property, not a list of three examples.

        ``as_text`` is the boundary between "a column that holds an
        identifier" and "a model that declares an identifier as text".
        Every value of a shape a column can hand back — a number, a
        boolean, a NULL, a list, a mapping, bytes, a tuple — has to be
        refused, because the alternative is a value that reaches the
        model under a signature that promised a string.

        The adversarial value is *drawn* from the strategy rather than
        hoped for: a strategy that might produce an acceptable value
        would make this a coin flip, and a test that only sometimes
        exercises the rule is a test that sometimes misses it.
        """
        with pytest.raises(TypeError):
            as_text(value)

    async def test_a_nullable_id_comes_back_as_none(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The call-site guard, on the column that is actually nullable."""
        cid = new_correlation_id()
        trace_id = await runs.start_run(correlation_id=cid, agent_role="planner")
        run = await runs.get_run(trace_id)
        assert run is not None
        assert run.span_parent_id is None


# ---------------------------------------------------------------------------
# The row → model conversion
# ---------------------------------------------------------------------------


class _ServerShapedBackend:
    """A backend whose row carries the shapes the *server* substrate produces.

    The embedded substrate stores an id as TEXT and hands back a ``str``;
    a PostgreSQL ``UUID`` column hands back a :class:`uuid.UUID`. The
    model declares the id as text, so the *reader* coerces — and a test
    that only ever runs against SQLite cannot see that coercion happen,
    because SQLite's value is already the right one.

    The fake answers every query with the one row it was given: it
    serves the read path under test and nothing else.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def fetch_one(self, sql: str, params: Any = ()) -> Any:
        return self._row

    async def fetch_all(self, sql: str, params: Any = ()) -> list[Any]:
        return [self._row]

    async def fetch_value(self, sql: str, params: Any = ()) -> Any:
        return None

    async def execute(self, sql: str, params: Any = ()) -> int:
        return 0

    async def execute_many(self, sql: str, rows: Any) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def table_names(self) -> list[str]:
        return ["hot_serving_execution_runs"]


def _server_shaped_row(**overrides: Any) -> dict[str, Any]:
    """A run row in the shape the server substrate hands back.

    ``trace_id`` is a ``UUID`` column there, so it arrives as a
    :class:`uuid.UUID`; ``started_at`` is a ``TIMESTAMPTZ``, so it
    arrives as a ``datetime`` rather than as the ISO text SQLite keeps.
    """
    row: dict[str, Any] = {
        "trace_id": uuid.uuid4(),
        "correlation_id": uuid.uuid4(),
        "session_id": None,
        "span_parent_id": None,
        "agent_role": "planner",
        "skill_name": "deploy",
        "modality": ComplexityModality.STANDARD.value,
        "permission_mode": PermissionMode.ASK.value,
        "started_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "ended_at": None,
        "duration_ms": None,
        "exit_code": None,
        "tokens_consumed": 0,
        "peak_rss_mb": None,
        "model_used": None,
        "escalated": 0,
        "telemetry": "{}",
        "error_detail": None,
    }
    row.update(overrides)
    return row


class TestTheRowConversion:
    """The conversion, on a row the embedded substrate cannot produce.

    These are the tests that would have caught the one-hop graph read
    that built its entity without coercing: a ``UUID`` column read as
    ``str`` by hand, on the substrate where the column *is* a ``UUID``.
    """

    async def test_a_uuid_trace_id_arrives_as_text(self) -> None:
        """The model declares the trace id as text, so it is text.

        Without the coercion the model carries a
        :class:`uuid.UUID` — which is not a string, and which
        ``json.dumps`` refuses.
        """
        trace_id = uuid.uuid4()
        backend = _ServerShapedBackend(_server_shaped_row(trace_id=trace_id))

        run = await ExecutionRunRepository(backend).get_run(str(trace_id))  # type: ignore[arg-type]

        assert run is not None
        assert run.trace_id == str(trace_id)
        assert isinstance(run.trace_id, str)

    async def test_a_uuid_parent_span_id_arrives_as_text(self) -> None:
        """The same coercion on the second id the row carries.

        The column is TEXT on the server substrate, so a UUID is not a
        shape it produces — but the model declares the field as text,
        and the reader is the thing that guarantees that. A value of
        another shape must not reach the model on the strength of a
        column type that happens to be narrow today.
        """
        span_id = uuid.uuid4()
        backend = _ServerShapedBackend(_server_shaped_row(span_parent_id=span_id))

        run = await ExecutionRunRepository(backend).get_run("trace")  # type: ignore[arg-type]

        assert run is not None
        assert run.span_parent_id == str(span_id)
        assert isinstance(run.span_parent_id, str)

    async def test_a_null_parent_span_id_stays_none(self) -> None:
        """A NULL is a NULL — not ``"None"`` and not ``""``.

        The nullable column is guarded at the call site, and the guard
        is what keeps the NULL a NULL: without it the coercion would be
        handed the NULL and would have to invent a value for it.
        """
        backend = _ServerShapedBackend(_server_shaped_row(span_parent_id=None))

        run = await ExecutionRunRepository(backend).get_run("trace")  # type: ignore[arg-type]

        assert run is not None
        assert run.span_parent_id is None
        assert run.to_trace_json()["parent_span_id"] is None

    async def test_the_run_record_is_still_json(self) -> None:
        """The trace record is written to a JSON document.

        A :class:`uuid.UUID` where a string was declared is not
        serialisable, and the failure surfaces wherever the telemetry is
        written — long after the run.
        """
        backend = _ServerShapedBackend(_server_shaped_row())

        run = await ExecutionRunRepository(backend).get_run("trace")  # type: ignore[arg-type]

        assert run is not None
        assert json.dumps(run.to_trace_json())


# ---------------------------------------------------------------------------
# The skill aggregate
# ---------------------------------------------------------------------------


class TestSkillStats:
    """The aggregate the optimiser reads before it rewrites anything.

    The rule the aggregate has to honour: **an empty sample is not a
    failure**. A skill with no evidence is a skill the optimiser must not
    rewrite, and a skill whose runs all failed is a different thing
    entirely. The two must never be reported the same way, because one of
    them is an instruction to act and the other is an absence of
    instructions.
    """

    async def test_a_skill_with_no_runs_has_no_samples(
        self, runs: ExecutionRunRepository
    ) -> None:
        stats = await runs.skill_stats("never-run")

        assert stats.skill_name == "never-run"
        assert stats.sample_count == 0
        assert stats.success_rate == 0.0
        assert stats.mean_latency_ms == 0.0
        assert stats.correction_rate == 0.0
        assert stats.escalation_rate == 0.0
        assert stats.mean_tokens == 0.0
        # The point of the zeroes: two runs is not enough to act on either.
        assert stats.is_significant is False

    async def test_an_unfinished_run_is_excluded_not_counted_as_a_failure(
        self, runs: ExecutionRunRepository
    ) -> None:
        """A run that has not ended is not a run that failed.

        Counting it would report a success rate of 0% for a skill that is
        simply still working, and the optimiser would read that as a
        verdict.
        """
        cid = new_correlation_id()
        await runs.start_run(
            correlation_id=cid, agent_role="worker", skill_name="slow"
        )

        stats = await runs.skill_stats("slow")

        assert stats.sample_count == 0
        assert stats.success_rate == 0.0
        assert stats.is_significant is False

    async def test_finished_runs_are_aggregated(self, runs: ExecutionRunRepository) -> None:
        """The numbers the optimiser actually acts on."""
        for index, exit_code in enumerate((0, 0, 1)):
            cid = new_correlation_id()
            trace_id = await runs.start_run(
                correlation_id=cid, agent_role="worker", skill_name="mixed"
            )
            await runs.finish_run(
                trace_id,
                exit_code=exit_code,
                tokens_consumed=100 * (index + 1),
                escalated=index == 2,
            )

        stats = await runs.skill_stats("mixed")

        assert stats.sample_count == 3
        assert stats.success_rate == pytest.approx(2 / 3)
        assert stats.escalation_rate == pytest.approx(1 / 3)
        assert stats.mean_tokens == pytest.approx(200.0)
        assert stats.mean_latency_ms >= 0.0
        assert stats.is_significant is False  # three runs is below the bar of five

    async def test_a_backend_that_answers_nothing_is_not_a_crash(self) -> None:
        """The aggregate query always returns a row — so a missing one is an anomaly.

        An aggregate without a ``GROUP BY`` returns exactly one row even
        when nothing matched, so a backend that answers nothing at all is
        not saying "no runs" — it is saying nothing. The reading is still
        the zeroed one, because the optimiser's rule is that an empty
        sample is not a failure, but it must not be a crash either.
        """

        class SilentBackend:
            async def fetch_one(self, sql: str, params: Any = ()) -> Any:
                return None

            async def fetch_all(self, sql: str, params: Any = ()) -> list[Any]:
                return []

            async def fetch_value(self, sql: str, params: Any = ()) -> Any:
                return None

            async def execute(self, sql: str, params: Any = ()) -> int:
                return 0

            async def execute_many(self, sql: str, rows: Any) -> None:
                return None

        stats = await ExecutionRunRepository(SilentBackend()).skill_stats("quiet")  # type: ignore[arg-type]

        assert stats.sample_count == 0
        assert stats.success_rate == 0.0
        assert stats.is_significant is False


# ---------------------------------------------------------------------------
# Closing a run
# ---------------------------------------------------------------------------


class TestFinishRun:
    """Closing a run, and the row that comes back.

    ``finish_run`` was the one method of this repository the suite used
    as *setup* and never asserted: every test that needed a finished run
    called it and moved on. So the method that writes the outcome — the
    exit code, the duration, the telemetry — had no test of its own, and
    the promise its docstring makes about a partial finish had none
    either.

    The promise is worth having, because the run row is *derived* state
    that an optimiser reads: a finish that erases what an earlier finish
    recorded is not a cosmetic loss, it is a skill's success rate
    changing because a caller was less informed the second time.
    """

    async def test_the_row_that_comes_back_is_the_row_that_was_written(
        self, runs: ExecutionRunRepository
    ) -> None:
        """Every field a finish accepts, asserted one by one.

        The return value was the gap: ``finish_run`` **returns** the
        completed row, and nothing ever looked at it. The returned row
        and the row a later reader sees have to be the same row, or the
        method is lying about what it did.
        """
        started = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        ended = started + timedelta(milliseconds=1500)
        correlation = new_correlation_id()
        session = new_session_id()
        parent_span = _new_span_id()
        trace_id = await runs.start_run(
            correlation_id=correlation,
            session_id=session,
            agent_role="coder",
            skill_name="deploy",
            span_parent_id=parent_span,
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.ASK,
            model_used="local-model",
            started_at=started,
        )

        finished = await runs.finish_run(
            trace_id,
            exit_code=0,
            telemetry={"counters": {"tokens": 3}},
            tokens_consumed=1234,
            peak_rss_mb=56.5,
            model_used="escalated-model",
            escalated=True,
            error_detail="a remark",
            ended_at=ended,
        )

        assert finished is not None
        assert finished.trace_id == trace_id
        assert finished.correlation_id == correlation
        assert finished.session_id == session
        assert finished.span_parent_id == parent_span
        assert finished.agent_role == "coder"
        assert finished.skill_name == "deploy"
        assert finished.modality == ComplexityModality.STANDARD.value
        assert finished.permission_mode == PermissionMode.ASK.value
        assert finished.started_at == started
        assert finished.ended_at == ended
        assert finished.duration_ms == 1500
        assert finished.exit_code == 0
        assert finished.tokens_consumed == 1234
        assert finished.peak_rss_mb == pytest.approx(56.5)
        assert finished.model_used == "escalated-model"
        assert finished.escalated is True
        assert finished.telemetry == {"counters": {"tokens": 3}}
        assert finished.error_detail == "a remark"
        assert finished.is_finished is True
        assert finished.status == "OK"

    async def test_the_returned_row_is_the_row_a_later_reader_sees(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The method returns what it wrote, not what it meant to write.

        The two are separated by a read: the finish writes and then
        re-reads. A write that silently failed would leave the returned
        row and the stored row different, and the returned row is what
        the caller acts on.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(), agent_role="worker"
        )

        finished = await runs.finish_run(trace_id, exit_code=0, tokens_consumed=7)
        stored = await runs.get_run(trace_id)

        assert finished is not None
        assert stored is not None
        assert finished == stored

    async def test_a_run_is_in_flight_until_it_is_finished(
        self, runs: ExecutionRunRepository
    ) -> None:
        """An open run is not a failed run.

        The status says so, and the status is what the optimiser reads:
        a crashed-mid-run process must not make a skill's success rate
        look catastrophic on the next boot.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(), agent_role="worker"
        )

        open_run = await runs.get_run(trace_id)

        assert open_run is not None
        assert open_run.is_finished is False
        assert open_run.status == "IN_FLIGHT"
        assert open_run.ended_at is None
        assert open_run.duration_ms is None

    async def test_a_non_zero_exit_code_is_an_error(self, runs: ExecutionRunRepository) -> None:
        """The other side of the same rule."""
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(), agent_role="worker"
        )

        finished = await runs.finish_run(trace_id, exit_code=2)

        assert finished is not None
        assert finished.status == "ERROR"
        assert finished.is_finished is True

    async def test_the_duration_comes_from_the_stored_start_not_the_caller(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The caller's stopwatch and the row's start time can disagree.

        A retry, a resumed run, a clock the caller measured before the
        insert — and only one of them is what the table says. So the
        duration is computed from the row's own ``started_at``, and the
        caller is not asked for it at all.
        """
        started = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(),
            agent_role="worker",
            started_at=started,
        )

        finished = await runs.finish_run(
            trace_id, exit_code=0, ended_at=started + timedelta(seconds=2, milliseconds=250)
        )

        assert finished is not None
        assert finished.duration_ms == 2250

    async def test_a_duration_is_never_negative(self, runs: ExecutionRunRepository) -> None:
        """A clock that went backwards does not produce a negative latency.

        The column is an INTEGER and a reader would report it as one, so
        a negative duration would read as a run that took less than no
        time at all. It is clamped rather than refused: the finish
        happened, and the row should say so.
        """
        started = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(),
            agent_role="worker",
            started_at=started,
        )

        finished = await runs.finish_run(
            trace_id, exit_code=0, ended_at=started - timedelta(seconds=5)
        )

        assert finished is not None
        assert finished.duration_ms == 0

    async def test_the_telemetry_round_trips_through_the_json_column(
        self, runs: ExecutionRunRepository
    ) -> None:
        """Telemetry is a JSON column, so telemetry is JSON.

        The column carries a ``json_valid`` constraint, and the value
        that reaches it is whatever the caller passed — a metrics
        snapshot, a dict of counters. A value that does not survive the
        round trip is a value the reader never sees.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(), agent_role="worker"
        )
        telemetry = {
            "counters": {"tokens": 12},
            "gauges": {},
            "histograms": {"graph_traversal_latency": {"count": 2, "p95": 4.8}},
        }

        finished = await runs.finish_run(trace_id, exit_code=0, telemetry=telemetry)

        assert finished is not None
        assert finished.telemetry == telemetry
        assert json.dumps(finished.telemetry)

    async def test_a_partial_finish_does_not_destroy_what_is_already_written(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The docstring's promise, as an assertion.

        *"Every optional parameter left as ``None`` preserves the
        existing value. ``COALESCE(?, column)`` in the UPDATE is what
        makes a partial finish (say, an exit code without token counts)
        non-destructive."*

        So: finish the run once, with everything; finish it again with
        only an exit code; and everything the first finish wrote must
        still be there. The second finish knows less than the first,
        and knowing less is not a reason to erase.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(),
            agent_role="worker",
            skill_name="deploy",
        )
        await runs.finish_run(
            trace_id,
            exit_code=0,
            tokens_consumed=120,
            peak_rss_mb=31.5,
            model_used="local-model",
            escalated=True,
            telemetry={"counters": {"x": 1}},
            error_detail="the first attempt complained",
        )

        second = await runs.finish_run(trace_id, exit_code=1)

        assert second is not None
        assert second.tokens_consumed == 120
        assert second.peak_rss_mb == pytest.approx(31.5)
        assert second.model_used == "local-model"
        assert second.escalated is True
        assert second.telemetry == {"counters": {"x": 1}}
        assert second.error_detail == "the first attempt complained"
        # The exit code the *second* finish supplied is the one that counts:
        # a value that was given is a value that is written.
        assert second.exit_code == 1

    async def test_a_finish_that_supplies_no_exit_code_does_not_erase_one(
        self, runs: ExecutionRunRepository
    ) -> None:
        """``exit_code=None`` means "not known", not "there was none".

        A caller that could not learn the exit code — a process that was
        killed, a worker that lost the thread — must not turn a
        recorded success into a failure by omitting the field. The
        ``COALESCE`` promise is about *omission*, and an omission is
        not a value.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(), agent_role="worker"
        )
        await runs.finish_run(trace_id, exit_code=0)

        second = await runs.finish_run(trace_id, exit_code=None)

        assert second is not None
        assert second.exit_code == 0
        assert second.status == "OK"

    async def test_a_run_that_ends_without_an_exit_code_is_an_error(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The *first* finish of a run has nothing to preserve.

        So ``exit_code=None`` on a run that has never been finished
        leaves the column empty — and a run that ended without saying how
        it ended is not a success. The preservation above must not turn
        this into ``OK``.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(), agent_role="worker"
        )

        finished = await runs.finish_run(trace_id, exit_code=None)

        assert finished is not None
        assert finished.exit_code is None
        assert finished.status == "ERROR"

    async def test_finishing_a_run_that_was_never_started_is_not_a_crash(
        self, runs: ExecutionRunRepository
    ) -> None:
        """An unknown trace id is a fact, not a failure.

        The caller has a trace id and the table does not have it — a
        purge ran, a database was restored, the id was made up. The
        method says so by returning ``None``, and it does not raise:
        the caller is closing a run it may never have opened.
        """
        finished = await runs.finish_run(str(uuid.uuid4()), exit_code=0)

        assert finished is None


# ---------------------------------------------------------------------------
# The filtered scan
# ---------------------------------------------------------------------------


#: The instant every seeded run is placed relative to.
_BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: The two correlation ids and the two session ids the population uses.
_CORRELATION_A = new_correlation_id()
_CORRELATION_B = new_correlation_id()
_SESSION_ONE = new_session_id()
_SESSION_TWO = new_session_id()


async def _seed_runs(runs: ExecutionRunRepository) -> dict[str, str]:
    """Four runs, each distinguishable from the others by one field.

    The population is small on purpose. A filter test asserts *which*
    rows come back, so every row has to be separable from every other
    by the field under test — a population of ten runs that all shared a
    skill name would make a skill-name filter vacuous.

    ``alpha`` and ``gamma`` are finished; ``beta`` is left open, so the
    ``only_finished`` filter has something to exclude.
    """
    ids: dict[str, str] = {}
    ids["alpha"] = await runs.start_run(
        correlation_id=_CORRELATION_A,
        session_id=_SESSION_ONE,
        agent_role="planner",
        skill_name="deploy",
        started_at=_BASE,
    )
    ids["beta"] = await runs.start_run(
        correlation_id=_CORRELATION_A,
        session_id=_SESSION_TWO,
        agent_role="worker",
        skill_name="deploy",
        started_at=_BASE + timedelta(hours=1),
    )
    ids["gamma"] = await runs.start_run(
        correlation_id=_CORRELATION_B,
        session_id=_SESSION_ONE,
        agent_role="planner",
        skill_name="test",
        started_at=_BASE + timedelta(hours=2),
    )
    ids["delta"] = await runs.start_run(
        correlation_id=_CORRELATION_B,
        session_id=_SESSION_TWO,
        agent_role="worker",
        skill_name="test",
        started_at=_BASE + timedelta(hours=3),
    )
    await runs.finish_run(ids["alpha"], exit_code=0)
    await runs.finish_run(ids["gamma"], exit_code=1)
    await runs.finish_run(ids["delta"], exit_code=0)
    return ids


def _names(rows: list[ExecutionRun], ids: dict[str, str]) -> list[str]:
    """The seeded names of the rows that came back, in the order they came."""
    by_trace = {trace: name for name, trace in ids.items()}
    return [by_trace[row.trace_id] for row in rows]


class TestQueryRuns:
    """The filtered scan — every filter, and the order it returns.

    Six filters, and each one is a different question. A filter that is
    silently ignored returns every row, and a filter that is
    mis-transcribed returns the wrong rows; both look the same to a
    reader who is not checking.
    """

    async def test_no_filter_returns_everything_newest_first(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The scan's own contract: newest first."""
        ids = await _seed_runs(runs)

        rows = await runs.query_runs()

        assert _names(rows, ids) == ["delta", "gamma", "beta", "alpha"]

    async def test_the_correlation_id_filter(self, runs: ExecutionRunRepository) -> None:
        """One lineage, and only that lineage."""
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(correlation_id=_CORRELATION_A)

        assert _names(rows, ids) == ["beta", "alpha"]

    async def test_the_session_id_filter(self, runs: ExecutionRunRepository) -> None:
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(session_id=_SESSION_ONE)

        assert _names(rows, ids) == ["gamma", "alpha"]

    async def test_the_skill_name_filter(self, runs: ExecutionRunRepository) -> None:
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(skill_name="deploy")

        assert _names(rows, ids) == ["beta", "alpha"]

    async def test_the_agent_role_filter(self, runs: ExecutionRunRepository) -> None:
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(agent_role="planner")

        assert _names(rows, ids) == ["gamma", "alpha"]

    async def test_the_since_filter(self, runs: ExecutionRunRepository) -> None:
        """``started_at`` at or after the given instant.

        The boundary is inclusive — ``>=`` — and it is inclusive for a
        reason: a retention sweep that asked for "everything since
        midnight" must not drop the run that started exactly at
        midnight.
        """
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(since=_BASE + timedelta(hours=2))

        assert _names(rows, ids) == ["delta", "gamma"]

        boundary = await runs.query_runs(since=_BASE + timedelta(hours=2))

        assert _names(boundary, ids) == ["delta", "gamma"]

    async def test_the_only_finished_filter(self, runs: ExecutionRunRepository) -> None:
        """The unfinished runs are excluded, not counted as failures."""
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(only_finished=True)

        assert _names(rows, ids) == ["delta", "gamma", "alpha"]

    async def test_the_filters_compose_as_and(self, runs: ExecutionRunRepository) -> None:
        """Two filters are two filters.

        The docstring says they compose as AND, and the query builds
        them as a list of clauses joined by AND — so this is the check
        that the list is joined the way it reads.
        """
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(skill_name="deploy", agent_role="planner")

        assert _names(rows, ids) == ["alpha"]

    async def test_the_limit_cuts_the_newest_rows_first(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The limit is applied to the ordered scan, so it keeps the newest.

        A limit applied before the ordering would keep an arbitrary
        three of four rows; the whole point of "newest first" is that
        the rows a reader sees are the ones that just happened.
        """
        ids = await _seed_runs(runs)

        rows = await runs.query_runs(limit=2)

        assert _names(rows, ids) == ["delta", "gamma"]

    async def test_a_filter_that_matches_nothing_returns_nothing(
        self, runs: ExecutionRunRepository
    ) -> None:
        """An empty answer is an answer.

        A filter for a skill that has never run returns no rows — and
        returns them as an empty list rather than raising, because
        "nothing matched" is the most ordinary result a query has.
        """
        await _seed_runs(runs)

        rows = await runs.query_runs(skill_name="never-run")

        assert rows == []

    async def test_the_order_is_total_when_two_runs_started_at_the_same_moment(
        self, runs: ExecutionRunRepository
    ) -> None:
        """A ``LIMIT`` cut needs a total order.

        ``started_at`` is not unique — two runs can start in the same
        microsecond — and an ``ORDER BY`` on a non-unique column leaves
        the cut to an order the SQL does not define. SQLite happens to
        be deterministic for a fixed plan (it returned the rows in
        *insertion* order here); PostgreSQL promises nothing about ties
        at all. The trace id, which is the primary key, breaks the tie,
        and the two substrates then agree on which rows a limit keeps.

        The runs are inserted in the *opposite* order to the one the
        rule names, so the test cannot pass by accident of insertion
        order.
        """
        same_moment = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        for trace_id in sorted([first, second], reverse=True):
            await runs.start_run(
                correlation_id=new_correlation_id(),
                agent_role="worker",
                trace_id=trace_id,
                started_at=same_moment,
            )

        rows = await runs.query_runs(limit=1)

        assert [row.trace_id for row in rows] == [min(first, second)]


class TestCount:
    async def test_the_count_is_the_number_of_rows(self, runs: ExecutionRunRepository) -> None:
        """Including the unfinished ones.

        The count is a count of rows, not of outcomes: an open run is
        evidence that a run started, and a count that hid it would
        under-report the work in flight.
        """
        assert await runs.count() == 0

        ids = await _seed_runs(runs)

        assert await runs.count() == len(ids)


class TestPurgeOlderThan:
    """The retention sweep, and the rule it must not break.

    The docstring claims: *"Unfinished runs are never purged regardless
    of age — an ancient in-flight row is evidence of an unresolved
    crash, which is the last thing a retention sweep should tidy
    away."* That claim had no test, and it is the kind of claim a
    retention sweep breaks by accident.
    """

    async def test_the_purge_removes_the_finished_runs_older_than_the_cutoff(
        self, runs: ExecutionRunRepository
    ) -> None:
        ids = await _seed_runs(runs)

        removed = await runs.purge_older_than(_BASE + timedelta(hours=2))

        assert removed == 1
        assert await runs.count() == 3
        remaining = {row.trace_id for row in await runs.query_runs()}
        assert ids["alpha"] not in remaining
        assert ids["gamma"] in remaining

    async def test_an_unfinished_run_is_never_purged_however_old_it_is(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The claim, as an assertion.

        ``beta`` is the oldest unfinished row in the population, and it
        is older than the cutoff by an hour. A purge that filtered on
        the age alone would delete it — and delete the evidence that a
        process died mid-run, which is the one row the recovery sweep
        exists to find.
        """
        ids = await _seed_runs(runs)

        removed = await runs.purge_older_than(_BASE + timedelta(hours=2))

        assert removed == 1
        remaining = {row.trace_id for row in await runs.query_runs()}
        assert ids["beta"] in remaining

    async def test_a_run_started_exactly_at_the_cutoff_survives(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The boundary is strict: ``started_at < cutoff``.

        A run that started exactly at the cutoff is not *older* than the
        cutoff, so it stays. The alternative — an inclusive boundary —
        would delete one row more than the caller asked for, and the
        difference between the two is invisible in the count.
        """
        trace_id = await runs.start_run(
            correlation_id=new_correlation_id(),
            agent_role="worker",
            started_at=_BASE,
        )
        await runs.finish_run(trace_id, exit_code=0)

        removed = await runs.purge_older_than(_BASE)

        assert removed == 0
        assert await runs.count() == 1

    async def test_the_purge_reports_how_many_rows_it_removed(
        self, runs: ExecutionRunRepository
    ) -> None:
        """The return value is the row count, not a boolean.

        A caller that sweeps on a schedule needs to know whether the
        sweep did anything, and ``0`` and ``1`` are the difference
        between "nothing to do" and "something was removed".
        """
        await _seed_runs(runs)

        assert await runs.purge_older_than(_BASE + timedelta(days=365)) == 3
        assert await runs.purge_older_than(_BASE + timedelta(days=365)) == 0
