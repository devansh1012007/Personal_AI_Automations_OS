"""The thread engine — and the C7.5 acceptance line, played back for real.

The spec's verification is *"a thread starts on harness A, swaps to B
mid-task, continues with context intact and the ledger unbroken"*. The
first test below is exactly that, with nothing mocked: a real ledger, the
real subprocess sandbox, the two shipped fake harnesses as real children,
and a real git repository as the workspace — so "context intact" is
asserted from the workspace's own files, the packets' own hashes and the
ledger's own chain, not from the engine's return values alone.

Around it: the lifecycle's honest edges — the recorded containment story
(``--record``), the interrupted close of a crash-dangled segment, the
worktree option, and every refusal the verbs can make.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from paa.config import PolicySettings
from paa.context.gatherer import ContextElement
from paa.core.types import EventType
from paa.ledger.store import LedgerStore
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.thread.engine import ThreadEngine
from paa.thread.errors import ThreadError
from paa.thread.handoff import read_packet
from paa.thread.record import Segment, ThreadStatus, save_thread
from tests.thread.conftest import FakeMemorySource

EXPECTED_CHAIN = [
    EventType.MANDATE_GRANTED,
    EventType.THREAD_CREATED,
    EventType.THREAD_SEGMENT_STARTED,
    EventType.THREAD_SEGMENT_ENDED,
    EventType.THREAD_HARNESS_SWAPPED,
    EventType.THREAD_SEGMENT_STARTED,
    EventType.THREAD_SEGMENT_ENDED,
]


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestTheSwapAcceptance:
    """The spec's verification: A → work → swap to B → work, context intact."""

    async def test_the_full_swap_line(
        self,
        engine: ThreadEngine,
        ledger: LedgerStore,
        paa_home: Path,
        git_repo: Path,
        policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="continue the notes across a swap",
            workspace=git_repo,
            policy=policy,
            default_harness="fake-alpha",
        )
        first = await engine.run_segment(record.id)
        second = await engine.swap(record.id, harness="fake-beta")

        # One thread, one mandate — the swap changed the hand, not the work.
        assert second.thread.id == record.id
        assert second.thread.mandate == record.mandate
        assert second.thread.title == record.title
        assert [s.harness for s in second.thread.segments] == [
            "fake-alpha",
            "fake-beta",
        ]
        assert first.exit_code == 0 and second.exit_code == 0
        assert not first.timed_out and not second.timed_out

        # Context intact, visible in the workspace's own files: beta's notes
        # line counts alpha's, and the work file carries both stints.
        notes = (git_repo / "thread-notes.md").read_text(encoding="utf-8").splitlines()
        assert len(notes) == 2
        assert notes[0].startswith("fake-alpha:") and "0 note line" in notes[0]
        assert notes[1].startswith("fake-beta:") and "1 note line" in notes[1]
        work = (git_repo / "alpha-work.txt").read_text(encoding="utf-8")
        assert "fake-alpha did a unit of work" in work
        assert "fake-beta continued the same unit of work" in work

        # The second stint's packet includes the first stint's effects: the
        # git summary hands beta the tree alpha left behind.
        packet_two = json.loads(second.packet_path.read_text(encoding="utf-8"))
        assert packet_two["git"]["available"] is True
        assert packet_two["git"]["dirty"] is True
        assert any("thread-notes.md" in line for line in packet_two["git"]["status"])
        assert any("alpha-work.txt" in line for line in packet_two["git"]["status"])
        # ... and the ledger tail hands beta the rows alpha's stint wrote.
        tail_types = [row["event_type"] for row in packet_two["ledger_tail"]]
        assert EventType.THREAD_SEGMENT_ENDED.value in tail_types
        assert EventType.THREAD_HARNESS_SWAPPED.value in tail_types

        # The packet-hash chain: each segment's recorded hash recomputes from
        # the persisted packet, and the two stints started from different
        # packets (the second one grew the context the first one produced).
        for segment in second.thread.segments:
            reread = read_packet(Path(segment.packet_path))
            assert reread.sha256() == segment.packet_sha256
        assert first.segment.packet_sha256 != second.segment.packet_sha256

        # The ledger: ordered, unbroken, one lineage under the thread id.
        events = await ledger.read_correlation(record.correlation_id())
        assert [e.event_type for e in events] == EXPECTED_CHAIN
        assert [e.state_version for e in events] == list(range(1, len(EXPECTED_CHAIN) + 1))
        ok, problems = await ledger.verify_chain(record.correlation_id())
        assert ok, problems
        # The swap row sits between the stints, by construction.
        assert events[4].payload["from_harness"] == "fake-alpha"
        assert events[4].payload["to_harness"] == "fake-beta"
        assert events[4].payload["after_segment"] == first.segment.segment_id

        # The fast handle agrees with the chain: reload from disk, no drift.
        reread_thread = engine.load(record.id)
        assert reread_thread == second.thread

    async def test_swap_to_the_same_harness_is_a_fresh_stint(
        self, engine: ThreadEngine, workspace: Path, policy: PolicySettings
    ) -> None:
        record = await engine.create_thread(
            title="same hand twice",
            workspace=workspace,
            policy=policy,
            default_harness="fake-alpha",
        )
        await engine.run_segment(record.id)
        outcome = await engine.swap(record.id, harness="fake-alpha")
        assert [s.harness for s in outcome.thread.segments] == [
            "fake-alpha",
            "fake-alpha",
        ]


class TestLedgerBeforeEffect:
    async def test_create_journals_the_grant_and_birth_before_the_file(
        self, engine: ThreadEngine, ledger: LedgerStore, paa_home: Path,
        workspace: Path, policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="born on the chain", workspace=workspace, policy=policy
        )
        events = await ledger.read_correlation(record.correlation_id())
        assert [e.event_type for e in events] == [
            EventType.MANDATE_GRANTED,
            EventType.THREAD_CREATED,
        ]
        # The grant row carries exactly the mandate the record holds.
        assert events[0].payload == record.mandate
        # The birth row attests the title without disclosing it.
        assert record.title not in json.dumps(events[1].payload)
        assert events[1].payload["title_sha256"] == _sha256_of_text(record.title)


class TestTheRecordedStory:
    async def test_a_recorded_segment_carries_its_evidence_pointer(
        self, engine: ThreadEngine, ledger: LedgerStore, workspace: Path,
        policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="recorded stint",
            workspace=workspace,
            policy=policy,
            default_harness="fake-alpha",
        )
        outcome = await engine.run_segment(record.id, record=True, timeout_s=60)
        assert outcome.exit_code == 0
        assert outcome.record_run_dir is not None
        assert outcome.record_run_dir.is_dir()

        segment = engine.load(record.id).segments[-1]
        assert segment.record_run_dir == str(outcome.record_run_dir)
        assert segment.exit_code == 0

        events = await ledger.read_correlation(record.correlation_id())
        ended = events[-1]
        assert ended.event_type is EventType.THREAD_SEGMENT_ENDED
        assert ended.payload["record_run_dir"] == str(outcome.record_run_dir)

    async def test_a_plain_segment_names_its_lesser_evidence_honestly(
        self, engine: ThreadEngine, ledger: LedgerStore, workspace: Path,
        policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="plain stint",
            workspace=workspace,
            policy=policy,
            default_harness="fake-alpha",
        )
        outcome = await engine.run_segment(record.id)
        assert outcome.record_run_dir is None
        events = await ledger.read_correlation(record.correlation_id())
        assert events[-1].payload["record_run_dir"] is None


class TestTheInterruptedClose:
    async def test_a_crash_dangled_segment_is_closed_as_interrupted(
        self, engine: ThreadEngine, ledger: LedgerStore, paa_home: Path,
        workspace: Path, policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="a crash mid-stint",
            workspace=workspace,
            policy=policy,
            default_harness="fake-alpha",
        )
        await engine.run_segment(record.id)

        # A crash: a segment opened and never closed, the file saved under it.
        thread = engine.load(record.id)
        thread.open_segment(
            Segment(
                segment_id="seg-crash",
                harness="fake-alpha",
                started_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                packet_sha256="b" * 64,
            )
        )
        save_thread(paa_home, thread)

        outcome = await engine.run_segment(record.id)
        segments = outcome.thread.segments
        assert [s.ended_reason for s in segments] == [
            "completed",
            "interrupted",
            "completed",
        ]
        assert segments[1].exit_code is None

        events = await ledger.read_correlation(record.correlation_id())
        # The interrupted close is journalled before the new stint starts.
        types = [e.event_type for e in events]
        assert types[4] is EventType.THREAD_SEGMENT_ENDED
        assert events[4].payload["reason"] == "interrupted"
        assert events[4].payload["segment_id"] == "seg-crash"
        assert types[5] is EventType.THREAD_SEGMENT_STARTED


class TestWorktrees:
    async def test_a_worktree_thread_runs_on_its_own_branch(
        self, engine: ThreadEngine, ledger: LedgerStore, paa_home: Path,
        git_repo: Path, policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="isolated tree",
            workspace=git_repo,
            policy=policy,
            default_harness="fake-alpha",
            worktree=True,
        )
        assert record.workspace == str(
            paa_home / "threads" / "worktrees" / record.id
        )
        outcome = await engine.run_segment(record.id)
        assert outcome.exit_code == 0
        # The harness's effects landed in the worktree, not the operator's tree.
        assert (Path(record.workspace) / "thread-notes.md").is_file()
        assert not (git_repo / "thread-notes.md").exists()

        branch = await asyncio.to_thread(
            lambda: subprocess.run(
                ["git", "-C", record.workspace, "branch", "--show-current"],
                capture_output=True,
                text=True,
            )
        )
        assert branch.stdout.strip() == f"paa-thread/{record.id}"
        status = await asyncio.to_thread(
            lambda: subprocess.run(
                ["git", "-C", str(git_repo), "status", "--porcelain"],
                capture_output=True,
                text=True,
            )
        )
        assert status.stdout.strip() == ""  # the operator's checkout stays clean


class TestMemoryHalf:
    async def test_recalled_facts_reach_the_packet(
        self, ledger: LedgerStore, paa_home: Path, workspace: Path,
        policy: PolicySettings,
    ) -> None:
        recalled = ContextElement(
            id="fact:1",
            content="the flaky test is order-dependent",
            relevance=0.9,
            confidence=0.8,
            importance=0.5,
            memory_domain="semantic",
        )
        engine = ThreadEngine(
            ledger=ledger,
            home=paa_home,
            sandbox=SubprocessSandbox(),
            memory_source=FakeMemorySource([recalled]),
        )
        record = await engine.create_thread(
            title="with memory", workspace=workspace, policy=policy,
            default_harness="fake-alpha",
        )
        outcome = await engine.run_segment(record.id)
        packet = json.loads(outcome.packet_path.read_text(encoding="utf-8"))
        contents = [element["content"] for element in packet["memory"]["elements"]]
        assert "the flaky test is order-dependent" in contents
        assert any(content.startswith("objective: ") for content in contents)


class TestRefusals:
    async def test_create_refuses_a_missing_workspace(
        self, engine: ThreadEngine, tmp_path: Path, policy: PolicySettings
    ) -> None:
        with pytest.raises(ThreadError, match="existing directory"):
            await engine.create_thread(
                title="nowhere", workspace=tmp_path / "nope", policy=policy
            )

    async def test_create_refuses_an_empty_title(
        self, engine: ThreadEngine, workspace: Path, policy: PolicySettings
    ) -> None:
        with pytest.raises(ThreadError, match="non-empty title"):
            await engine.create_thread(title="   ", workspace=workspace, policy=policy)

    async def test_create_refuses_an_unknown_default_harness(
        self, engine: ThreadEngine, workspace: Path, policy: PolicySettings
    ) -> None:
        with pytest.raises(ThreadError, match="unknown harness"):
            await engine.create_thread(
                title="x", workspace=workspace, policy=policy, default_harness="cursor"
            )

    async def test_run_refuses_an_unknown_harness(
        self, engine: ThreadEngine, workspace: Path, policy: PolicySettings
    ) -> None:
        record = await engine.create_thread(
            title="x", workspace=workspace, policy=policy, default_harness="fake-alpha"
        )
        with pytest.raises(ThreadError, match="unknown harness"):
            await engine.run_segment(record.id, harness="codex")

    async def test_run_refuses_when_no_harness_is_resolvable(
        self, engine: ThreadEngine, workspace: Path, policy: PolicySettings
    ) -> None:
        record = await engine.create_thread(title="x", workspace=workspace, policy=policy)
        with pytest.raises(ThreadError, match="no harness to run"):
            await engine.run_segment(record.id)

    async def test_swap_refuses_before_any_segment_exists(
        self, engine: ThreadEngine, workspace: Path, policy: PolicySettings
    ) -> None:
        record = await engine.create_thread(
            title="x", workspace=workspace, policy=policy, default_harness="fake-alpha"
        )
        with pytest.raises(ThreadError, match="nothing to swap from"):
            await engine.swap(record.id, harness="fake-beta")

    async def test_a_closed_thread_refuses_further_segments(
        self, engine: ThreadEngine, paa_home: Path, workspace: Path,
        policy: PolicySettings,
    ) -> None:
        record = await engine.create_thread(
            title="done", workspace=workspace, policy=policy,
            default_harness="fake-alpha",
        )
        thread = engine.load(record.id)
        thread.status = ThreadStatus.CLOSED
        save_thread(paa_home, thread)
        with pytest.raises(ThreadError, match="closed"):
            await engine.run_segment(record.id)

    async def test_run_refuses_an_unknown_thread(self, engine: ThreadEngine) -> None:
        with pytest.raises(ThreadError, match="no readable thread record"):
            await engine.run_segment(str(uuid.uuid4()))

    async def test_load_refuses_an_unknown_thread(self, engine: ThreadEngine) -> None:
        with pytest.raises(ThreadError):
            engine.load(str(uuid.uuid4()))
