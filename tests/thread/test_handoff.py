"""The handoff packet — how "context intact" became checkable (C7.5).

What is pinned here
-------------------
* :func:`assemble_handoff` is pure: same inputs, same canonical bytes, same
  sha256 — twice in a row, with dicts built in different insertion orders,
  and with no clock in the packet at all. A different segment id or a
  different thread is a different hash.
* The four inputs ride in their own fields: the mandate verbatim, a
  compiled gatherer packet whose first-class element is the thread's own
  objective (the ``objective`` slot is never vacant), the ledger tail as
  bounded identity rows (newest 25, oldest first), and the git summary
  (``None`` stated as ``available: False``, not hidden).
* :func:`collect_git_summary` asks git through the sandbox — the observer's
  one git vocabulary — and reports a real repo's HEAD/status/log, bounded,
  with a non-repo stated as such.
* Persistence round-trips: ``save_packet``/``read_packet`` with the
  recomputed hash matching, and a foreign schema tag refused.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from paa.context.gatherer import BoundedContextGatherer, ContextElement
from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.thread.errors import ThreadError
from paa.thread.handoff import (
    GIT_SUMMARY_LINES,
    LEDGER_TAIL_ROWS,
    assemble_handoff,
    collect_git_summary,
    packet_path,
    read_packet,
    save_packet,
)
from tests.thread.conftest import make_thread


class TestPurityAndHashing:
    def test_same_inputs_give_the_same_hash(self, gatherer: BoundedContextGatherer) -> None:
        thread = make_thread()
        one = assemble_handoff(thread=thread, segment_id="seg-1", gatherer=gatherer)
        two = assemble_handoff(thread=thread, segment_id="seg-1", gatherer=gatherer)
        assert one.canonical_bytes() == two.canonical_bytes()
        assert one.sha256() == two.sha256()

    def test_dict_insertion_order_does_not_change_the_bytes(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        thread = make_thread()
        reordered = make_thread(mandate=dict(reversed(list(thread.mandate.items()))))
        one = assemble_handoff(thread=thread, segment_id="seg-1", gatherer=gatherer)
        two = assemble_handoff(thread=reordered, segment_id="seg-1", gatherer=gatherer)
        assert one.canonical_bytes() == two.canonical_bytes()

    def test_canonical_bytes_are_the_ledgers_own_canonical_json(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        """One serialisation vocabulary: the packet hashes with the ledger's
        own :func:`canonical_json` — a second way of hashing would drift."""
        from paa.ledger.events import canonical_json

        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-1", gatherer=gatherer
        )
        raw = packet.canonical_bytes()
        assert raw == canonical_json(packet.to_json()).encode("utf-8")
        assert raw.startswith(b'{"git":')  # sorted keys, compact, no wrapper
        assert json.loads(raw) == packet.to_json()

    def test_a_different_segment_is_a_different_hash(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        thread = make_thread()
        one = assemble_handoff(thread=thread, segment_id="seg-1", gatherer=gatherer)
        two = assemble_handoff(thread=thread, segment_id="seg-2", gatherer=gatherer)
        assert one.sha256() != two.sha256()

    def test_a_different_thread_is_a_different_hash(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        one = assemble_handoff(thread=make_thread(), segment_id="seg-1", gatherer=gatherer)
        other = make_thread(title="a different mandate entirely")
        two = assemble_handoff(thread=other, segment_id="seg-1", gatherer=gatherer)
        assert one.sha256() != two.sha256()

    def test_the_hash_is_a_plain_sha256_of_the_canonical_bytes(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        import hashlib

        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-1", gatherer=gatherer
        )
        assert packet.sha256() == hashlib.sha256(packet.canonical_bytes()).hexdigest()


class TestTheFourInputs:
    def test_the_mandate_rides_verbatim(self, gatherer: BoundedContextGatherer) -> None:
        thread = make_thread()
        packet = assemble_handoff(thread=thread, segment_id="seg-1", gatherer=gatherer)
        assert packet.mandate == thread.mandate
        assert packet.to_json()["mandate"] == thread.mandate

    def test_the_objective_is_the_first_class_memory_element(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(title="ship the swap"), segment_id="seg-1", gatherer=gatherer
        )
        memory = packet.memory
        ids = [element["id"] for element in memory["elements"]]
        assert ids == ["thread:11111111-2222-3333-4444-555555555555:mandate"]
        objective = memory["elements"][0]
        assert objective["slot"] == "objective"
        assert objective["content"] == "objective: ship the swap under policy ask-v3"
        # The required slot resolved — the objective survives any noise.
        assert memory["vacant_slots"] == []

    def test_recalled_candidates_join_the_memory_half(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        recalled = ContextElement(
            id="fact:1",
            content="the build breaks on stale caches",
            relevance=0.9,
            confidence=0.8,
            importance=0.5,
            memory_domain="semantic",
        )
        packet = assemble_handoff(
            thread=make_thread(),
            segment_id="seg-1",
            gatherer=gatherer,
            memory_elements=[recalled],
        )
        contents = [element["content"] for element in packet.memory["elements"]]
        assert "the build breaks on stale caches" in contents

    def test_the_ledger_tail_rides_as_bounded_identity_rows(
        self, ledger: LedgerStore, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(),
            segment_id="seg-1",
            gatherer=gatherer,
        )
        assert packet.ledger_tail == ()
        assert packet.to_json()["ledger_tail"] == []

    def test_a_gatherer_without_a_memory_half_is_honest_about_it(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-1", gatherer=gatherer
        )
        # No memory source: the objective still rides, nothing is fabricated.
        assert len(packet.memory["elements"]) == 1

    def test_git_summary_none_is_stated_not_hidden(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-1", gatherer=gatherer, git_summary=None
        )
        assert packet.git == {"available": False}

    def test_git_summary_rides_verbatim(self, gatherer: BoundedContextGatherer) -> None:
        summary = {"available": True, "head": "a" * 40, "dirty": True}
        packet = assemble_handoff(
            thread=make_thread(),
            segment_id="seg-1",
            gatherer=gatherer,
            git_summary=summary,
        )
        assert packet.git == summary


class TestTheLedgerTail:
    async def test_the_tail_is_the_newest_rows_oldest_first(
        self, ledger: LedgerStore, gatherer: BoundedContextGatherer
    ) -> None:
        cid = uuid.uuid4()
        for i in range(LEDGER_TAIL_ROWS + 5):
            await ledger.append(
                LedgerEvent.create(
                    cid,
                    EventType.RECOVERY_REPLAY_COMPLETED,
                    payload={"v": 1, "n": i},
                    discriminator=f"tail-{i}",  # distinct ADR-0008 keys, not one
                )
            )
        events = await ledger.read_correlation(cid)
        packet = assemble_handoff(
            thread=make_thread(),
            segment_id="seg-1",
            gatherer=gatherer,
            ledger_tail=events,
        )
        assert len(packet.ledger_tail) == LEDGER_TAIL_ROWS
        versions = [row["state_version"] for row in packet.ledger_tail]
        assert versions == sorted(versions)  # oldest first
        assert versions[0] == 6  # the newest 25 of 30, no off-by-one
        # Identity rows carry the hash, not the payload — the stronger fact.
        assert all(row["event_hash"] for row in packet.ledger_tail)
        assert all(
            set(row) == {"state_version", "event_type", "event_hash"}
            for row in packet.ledger_tail
        )

    async def test_an_unsealed_event_rides_with_a_null_hash_rather_than_vanishing(
        self, gatherer: BoundedContextGatherer
    ) -> None:
        cid = uuid.uuid4()
        unsealed = LedgerEvent.create(
            cid, EventType.THREAD_CREATED, payload={"v": 1}
        )
        packet = assemble_handoff(
            thread=make_thread(),
            segment_id="seg-1",
            gatherer=gatherer,
            ledger_tail=[unsealed],
        )
        assert packet.ledger_tail[0]["event_hash"] is None


class TestCollectGitSummary:
    async def test_a_real_repository_reports_head_status_and_log(
        self, git_repo: Path, sandbox: SubprocessSandbox
    ) -> None:
        summary = await collect_git_summary(sandbox, git_repo)
        assert summary["available"] is True
        assert summary["dirty"] is False
        assert len(summary["head"]) == 40
        assert summary["recent_commits"] == [f"{summary['head'][:7]} seed"]
        assert summary["status"] == []

    async def test_a_touched_tree_is_dirty_with_bounded_status(
        self, git_repo: Path, sandbox: SubprocessSandbox
    ) -> None:
        for i in range(GIT_SUMMARY_LINES + 5):
            (git_repo / f"file-{i:02d}.txt").write_text("x\n", encoding="utf-8")
        summary = await collect_git_summary(sandbox, git_repo)
        assert summary["dirty"] is True
        assert len(summary["status"]) == GIT_SUMMARY_LINES
        assert summary["status_truncated"] is True
        assert any("file-00.txt" in line for line in summary["status"])

    async def test_a_plain_directory_is_stated_as_not_a_worktree(
        self, workspace: Path, sandbox: SubprocessSandbox
    ) -> None:
        summary = await collect_git_summary(sandbox, workspace)
        assert summary["available"] is False
        assert summary["reason"] == "not a git worktree"


class TestPersistence:
    def test_save_then_read_round_trips_and_the_hash_recomputes(
        self, paa_home: Path, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-7", gatherer=gatherer
        )
        path = save_packet(paa_home, packet)
        assert path == packet_path(paa_home, packet.thread_id, packet.segment_id)

        reread = read_packet(path)
        assert reread.thread_id == packet.thread_id
        assert reread.segment_id == packet.segment_id
        assert reread.mandate == packet.mandate
        assert reread.memory == packet.memory
        assert reread.git == packet.git
        # The recomputation is over the parsed fields — a real content check.
        assert reread.sha256() == packet.sha256()

    def test_saved_json_carries_the_identity_tag(
        self, paa_home: Path, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-7", gatherer=gatherer
        )
        raw = json.loads(save_packet(paa_home, packet).read_text(encoding="utf-8"))
        assert raw["schema"] == "paa-handoff/1"

    def test_save_leaves_no_torn_tmp_behind(
        self, paa_home: Path, gatherer: BoundedContextGatherer
    ) -> None:
        packet = assemble_handoff(
            thread=make_thread(), segment_id="seg-7", gatherer=gatherer
        )
        path = save_packet(paa_home, packet)
        assert list(path.parent.glob("*.tmp")) == []

    def test_read_refuses_an_absent_packet(self, tmp_path: Path) -> None:
        with pytest.raises(ThreadError, match="no readable handoff packet"):
            read_packet(tmp_path / "nope.json")

    def test_read_refuses_a_foreign_schema_tag(self, tmp_path: Path) -> None:
        path = tmp_path / "foreign.json"
        path.write_text(json.dumps({"schema": "paa-handoff/2"}))
        with pytest.raises(ThreadError, match="not a paa-handoff/1 handoff packet"):
            read_packet(path)
