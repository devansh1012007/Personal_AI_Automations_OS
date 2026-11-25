"""The thread record's invariants and persistence (C7.5).

What is pinned here
-------------------
* The atomic JSON contract under ``PAA_HOME/threads/<id>.json``: the
  ``paa-thread/1`` identity tag, the save/load round-trip, no torn
  ``.tmp`` leftovers, and the refusal (never reinterpretation) of absent,
  foreign and malformed files.
* The append-mostly invariants as *methods*: a duplicate segment id is
  refused, an already-ended segment is history that cannot be rewritten,
  an unknown id cannot be closed, an unknown reason is not a reason.
* ``list_threads`` order and its refusal to quietly drop a broken file —
  a list that lies about what exists is worse than no list.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from paa.thread.errors import ThreadError
from paa.thread.record import (
    THREAD_RECORD_SCHEMA,
    Segment,
    ThreadRecord,
    ThreadStatus,
    list_threads,
    load_thread,
    save_thread,
    thread_path,
)
from tests.thread.conftest import make_thread


def _segment(segment_id: str = "seg-1") -> Segment:
    return Segment(
        segment_id=segment_id,
        harness="fake-alpha",
        started_at="2026-11-09T00:00:01.000+00:00",
        packet_sha256="a" * 64,
    )


def _close(
    record: ThreadRecord,
    segment_id: str,
    *,
    reason: str = "completed",
    ended_at: str = "2026-11-09T00:00:09.000+00:00",
) -> Segment:
    return record.close_segment(
        segment_id,
        ended_at=ended_at,
        reason=reason,
        exit_code=None if reason == "interrupted" else 0,
        timed_out=False,
        duration_ms=8000.0,
        record_run_dir=None,
    )


class TestPersistence:
    def test_save_writes_the_identity_tag_under_threads(self, paa_home: Path) -> None:
        record = make_thread()
        path = save_thread(paa_home, record)
        assert path == thread_path(paa_home, record.id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["schema"] == THREAD_RECORD_SCHEMA == "paa-thread/1"

    def test_save_creates_the_threads_directory_on_first_use(self, paa_home: Path) -> None:
        assert not (paa_home / "threads").exists()
        save_thread(paa_home, make_thread())
        assert (paa_home / "threads").is_dir()

    def test_save_leaves_no_torn_tmp_behind(self, paa_home: Path) -> None:
        """Atomic write discipline: temp beside the target, then replace."""
        path = save_thread(paa_home, make_thread())
        assert path.is_file()
        assert list(path.parent.glob("*.tmp")) == []

    def test_save_then_load_round_trips(self, paa_home: Path) -> None:
        record = make_thread()
        record.open_segment(_segment())
        save_thread(paa_home, record)

        loaded = load_thread(paa_home, record.id)
        assert loaded == record
        assert loaded.segments[0].harness == "fake-alpha"

    def test_load_refuses_an_absent_thread(self, paa_home: Path) -> None:
        with pytest.raises(ThreadError, match="no readable thread record"):
            load_thread(paa_home, str(uuid.uuid4()))

    def test_load_refuses_a_foreign_schema_tag(self, paa_home: Path) -> None:
        record = make_thread()
        path = thread_path(paa_home, record.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "paa-thread/2", "id": record.id}))
        with pytest.raises(ThreadError, match="not a paa-thread/1 thread record"):
            load_thread(paa_home, record.id)

    def test_load_refuses_a_malformed_body(self, paa_home: Path) -> None:
        record = make_thread()
        path = thread_path(paa_home, record.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": THREAD_RECORD_SCHEMA})  # no id, no mandate, no body
        )
        with pytest.raises(ThreadError, match="does not validate"):
            load_thread(paa_home, record.id)


class TestSegmentInvariants:
    def test_open_segment_appends_in_order(self) -> None:
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        record.open_segment(_segment("seg-2"))
        assert [s.segment_id for s in record.segments] == ["seg-1", "seg-2"]

    def test_open_segment_bumps_updated_at(self) -> None:
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        assert record.updated_at >= record.created_at

    def test_open_segment_refuses_a_duplicate_id(self) -> None:
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        with pytest.raises(ThreadError, match="already exists"):
            record.open_segment(_segment("seg-1"))

    def test_close_segment_fills_only_the_end_fields(self) -> None:
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        closed = _close(record, "seg-1")
        assert closed.ended_reason == "completed"
        assert closed.exit_code == 0
        assert closed.duration_ms == 8000.0
        assert not closed.is_open
        # The opened half is untouched: append-mostly, one method each.
        assert closed.harness == "fake-alpha"
        assert closed.started_at == "2026-11-09T00:00:01.000+00:00"
        assert closed.packet_sha256 == "a" * 64

    def test_close_segment_refuses_an_unknown_id(self) -> None:
        record = make_thread()
        with pytest.raises(ThreadError, match="no such segment"):
            _close(record, "nope")

    def test_close_segment_refuses_a_second_close(self) -> None:
        """History is never rewritten — the torn-write class, caught here."""
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        _close(record, "seg-1")
        with pytest.raises(ThreadError, match="history is never rewritten"):
            _close(record, "seg-1", reason="interrupted")

    def test_close_segment_refuses_an_unknown_reason(self) -> None:
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        with pytest.raises(ThreadError, match="unknown segment end reason"):
            _close(record, "seg-1", reason="cancelled")

    def test_last_open_segment_finds_the_trailing_open_stint(self) -> None:
        record = make_thread()
        record.open_segment(_segment("seg-1"))
        assert record.last_open_segment() is not None
        _close(record, "seg-1")
        assert record.last_open_segment() is None

    def test_a_closed_status_survives_the_round_trip(self, paa_home: Path) -> None:
        record = make_thread()
        record.status = ThreadStatus.CLOSED
        save_thread(paa_home, record)
        assert load_thread(paa_home, record.id).status is ThreadStatus.CLOSED


class TestListing:
    def test_list_is_empty_when_no_threads_directory_exists(self, paa_home: Path) -> None:
        assert list_threads(paa_home) == []

    def test_list_orders_oldest_created_first(self, paa_home: Path) -> None:
        older = make_thread()
        newer = make_thread()
        newer.id = "99999999-2222-3333-4444-555555555555"
        newer.created_at = newer.updated_at = "2026-11-10T00:00:00.000+00:00"
        # Written newest-first on disk: order must come from created_at, not the glob.
        save_thread(paa_home, newer)
        save_thread(paa_home, older)
        assert [r.id for r in list_threads(paa_home)] == [older.id, newer.id]

    def test_list_ignores_a_torn_tmp_leftover(self, paa_home: Path) -> None:
        record = make_thread()
        save_thread(paa_home, record)
        (paa_home / "threads" / f"{record.id}.json.tmp").write_text("{")
        assert [r.id for r in list_threads(paa_home)] == [record.id]

    def test_list_refuses_to_drop_a_broken_file(self, paa_home: Path) -> None:
        """A list that quietly skips a broken record lies about what exists."""
        save_thread(paa_home, make_thread())
        (paa_home / "threads" / "broken.json").write_text("not json at all")
        with pytest.raises(ThreadError):
            list_threads(paa_home)


class TestIdentity:
    def test_the_thread_id_is_the_correlation_id(self) -> None:
        record = make_thread()
        assert record.correlation_id() == uuid.UUID(record.id)

    def test_a_hand_edited_non_uuid_id_refuses_at_the_ledger_key(
        self, paa_home: Path
    ) -> None:
        """The fast handle tolerates a str id; the lineage key does not.

        The file is the projection and the ledger is the truth — so the
        refusal belongs to ``correlation_id()`` (the door to the ledger),
        not to ``load_thread`` (the door to the handle).
        """
        record = make_thread()
        raw = {"schema": THREAD_RECORD_SCHEMA, **record.model_dump(mode="json")}
        raw["id"] = "not-a-uuid"
        path = thread_path(paa_home, "not-a-uuid")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw))

        loaded = load_thread(paa_home, "not-a-uuid")
        with pytest.raises(ValueError):
            loaded.correlation_id()
