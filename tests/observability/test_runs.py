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
from datetime import UTC, datetime
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
from paa.observability.runs import ExecutionRunRepository, InvalidTraceIdError
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
