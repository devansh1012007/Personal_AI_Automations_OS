"""N2.3: crash recovery for coder sessions (sweep closure + exemption).

What is pinned here
-------------------
* The boot sweep closes a ``TOOL_CALL_PROPOSED`` with no outcome as
  ``TOOL_CALL_FAILED(reason=interrupted)`` — golden v1 FAILED shape,
  no new payload field — and the close is idempotent across sweeps.
* Coder-only lineages (only ``CODER_SESSION_*`` / ``TOOL_CALL_*`` /
  ``MANDATE_*`` audit events) are never re-queued as tasks
  (``SKIPPED_CODER_LINEAGE``); mixed lineages keep the ordinary task
  path but still close their interrupted coder calls.
* ``classify_interrupted`` from persisted intent: a file edit matching
  ``post`` is ``applied``, matching ``pre`` (or a creation with no
  file) is ``not_applied`` (the brief's ``pending``), everything else
  — shell / git / ``apply_patch``, an unmakeable prediction, a file
  matching neither hash — is ``unknown`` (D-6: never auto-repeat).
* Kill simulation: a lineage written and then the database closed and
  reopened loses nothing — the sweep closes exactly the unclosed call,
  the chain still verifies, and a second sweep appends nothing.
* ``--resume``: a resumed session's interrupted calls are closed and
  named, with their classifications, in one ``SYSTEM`` recovery note
  (``Session.inject_recovery_note``) — file edits classified precisely
  by the transcript's recorded path, everything else ``unknown``.
* ``DENIED`` without a ``PROPOSED`` (the hook-block path) is proof of
  non-execution, never an interrupted call.

The pure helpers (``find_interrupted_calls``,
``classify_interrupted``) run against hand-built events; the engine
paths run against a real ``LedgerStore`` on a throwaway database, the
same shape as ``tests/ledger/test_recovery.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from paa.coder.agent import CoderAgent
from paa.coder.journal import args_sha256_of
from paa.coder.loop import ModelTurn
from paa.coder.permissions import PermissionMode
from paa.coder.session import Session, TurnRole
from paa.coder.workspace import Workspace
from paa.config import Settings
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.recovery import (
    RecoveryEngine,
    RecoveryOutcome,
    classify_interrupted,
    find_interrupted_calls,
)
from paa.ledger.store import LedgerStore
from paa.storage.relational.database import Database
from tests.conftest import RecordingRequeuer


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_OUTCOME_KIND = {
    EventType.TOOL_CALL_DENIED: "DENIED",
    EventType.TOOL_CALL_COMPLETED: "COMPLETED",
    EventType.TOOL_CALL_FAILED: "FAILED",
}


def _proposed_payload(
    session_id: str,
    call_id: str,
    tool: str,
    *,
    pre: str | None = None,
    post: str | None = None,
    patch: str | None = None,
    args: dict | None = None,
) -> dict:
    return {
        "v": 1,
        "session_id": session_id,
        "call_id": call_id,
        "tool": tool,
        "args_sha256": args_sha256_of(args or {"path": "note.txt", "content": "after"}),
        "args_preview_redacted": f"{tool}(...)",
        "isolation_level": "NONE",
        "pre_sha256": pre,
        "post_sha256": post,
        "patch_sha256": patch,
    }


async def _proposed(
    ledger: LedgerStore,
    cid: uuid.UUID,
    session_id: str,
    call_id: str,
    tool: str,
    **kw,
) -> None:
    await ledger.append(
        LedgerEvent.create(
            cid,
            EventType.TOOL_CALL_PROPOSED,
            attempt=0,
            discriminator=f"{session_id}:{call_id}:PROPOSED",
            payload=_proposed_payload(session_id, call_id, tool, **kw),
        )
    )


async def _outcome(
    ledger: LedgerStore,
    cid: uuid.UUID,
    session_id: str,
    call_id: str,
    etype: EventType,
) -> None:
    payload: dict = {
        "v": 1,
        "session_id": session_id,
        "call_id": call_id,
        "tool": "write_file",
    }
    if etype is EventType.TOOL_CALL_DENIED:
        payload["reason"] = "permission denied"
    elif etype is EventType.TOOL_CALL_COMPLETED:
        payload.update(
            {"result_sha256": "e" * 64, "args_sha256": "a" * 64, "isolation_level": "NONE"}
        )
    else:
        payload.update(
            {
                "reason": "boom",
                "result_sha256": None,
                "args_sha256": "a" * 64,
                "isolation_level": "NONE",
            }
        )
    await ledger.append(
        LedgerEvent.create(
            cid,
            etype,
            attempt=0,
            discriminator=f"{session_id}:{call_id}:{_OUTCOME_KIND[etype]}",
            payload=payload,
        )
    )


class TestClassifyInterrupted:
    """The persisted-intent verdict, on its own terms (pure function)."""

    def test_live_matching_post_is_applied(self) -> None:
        state = classify_interrupted(
            "write_file",
            pre_sha256="b" * 64,
            post_sha256="c" * 64,
            patch_sha256=None,
            live_sha256="c" * 64,
            live_checked=True,
        )
        assert state.value == "applied"

    def test_live_matching_pre_is_not_applied(self) -> None:
        state = classify_interrupted(
            "edit_file",
            pre_sha256="b" * 64,
            post_sha256="c" * 64,
            patch_sha256=None,
            live_sha256="b" * 64,
            live_checked=True,
        )
        assert state.value == "not_applied"

    def test_absent_file_for_a_creation_is_not_applied(self) -> None:
        state = classify_interrupted(
            "write_file",
            pre_sha256=None,  # creation: there was no file at PROPOSED time
            post_sha256="c" * 64,
            patch_sha256=None,
            live_sha256=None,  # still no file: the tool never landed
            live_checked=True,
        )
        assert state.value == "not_applied"

    def test_absent_file_for_an_edit_is_unknown(self) -> None:
        # The file existed at PROPOSED time and is gone now: something
        # deleted it — not a verdict either way.
        state = classify_interrupted(
            "edit_file",
            pre_sha256="b" * 64,
            post_sha256="c" * 64,
            patch_sha256=None,
            live_sha256=None,
            live_checked=True,
        )
        assert state.value == "unknown"

    def test_neither_hash_is_unknown(self) -> None:
        state = classify_interrupted(
            "write_file",
            pre_sha256="b" * 64,
            post_sha256="c" * 64,
            patch_sha256=None,
            live_sha256="z" * 64,  # torn write / CRLF divergence / later overwrite
            live_checked=True,
        )
        assert state.value == "unknown"

    def test_unmakeable_post_is_never_guessed(self) -> None:
        state = classify_interrupted(
            "edit_file",
            pre_sha256="b" * 64,
            post_sha256=None,  # journal predicts None when the tool would refuse
            patch_sha256=None,
            live_sha256="b" * 64,
            live_checked=True,
        )
        assert state.value == "unknown"

    @pytest.mark.parametrize(
        "tool", ["bash", "git_commit", "git_stage", "apply_patch", "read_file"]
    )
    def test_non_file_tools_are_always_unknown(self, tool: str) -> None:
        # apply_patch carries only a digest; shell/git effects cannot be
        # proven from hashes. D-6: unknown, never auto-repeat.
        state = classify_interrupted(
            tool,
            pre_sha256=None,
            post_sha256=None,
            patch_sha256="d" * 64,
            live_sha256="c" * 64,
            live_checked=True,
        )
        assert state.value == "unknown"

    def test_unchecked_live_is_unknown_even_for_a_file_edit(self) -> None:
        # The sweep's ledger-only view: no path, no live hash, no guess.
        state = classify_interrupted(
            "write_file",
            pre_sha256="b" * 64,
            post_sha256="c" * 64,
            patch_sha256=None,
        )
        assert state.value == "unknown"

    def test_identical_rewrite_reads_applied(self) -> None:
        # pre == post (same content rewritten): the live file matches,
        # and re-running would be a no-op anyway.
        state = classify_interrupted(
            "write_file",
            pre_sha256="b" * 64,
            post_sha256="b" * 64,
            patch_sha256=None,
            live_sha256="b" * 64,
            live_checked=True,
        )
        assert state.value == "applied"


class TestFindInterruptedCalls:
    """PROPOSED-without-outcome detection (pure function over events)."""

    def _proposed_event(self, cid, session_id="sess-1", call_id="call-1", tool="write_file"):
        return LedgerEvent(
            correlation_id=cid,
            event_type=EventType.TOOL_CALL_PROPOSED,
            payload=_proposed_payload(session_id, call_id, tool),
        )

    def _outcome_event(self, cid, etype, session_id="sess-1", call_id="call-1"):
        payload = {"v": 1, "session_id": session_id, "call_id": call_id, "tool": "write_file"}
        return LedgerEvent(correlation_id=cid, event_type=etype, payload=payload)

    def test_proposed_without_outcome_is_interrupted(self) -> None:
        cid = new_correlation_id()
        found = find_interrupted_calls([self._proposed_event(cid)])
        assert [(c.session_id, c.call_id, c.tool) for c in found] == [
            ("sess-1", "call-1", "write_file")
        ]
        assert found[0].correlation_id == cid

    def test_each_outcome_kind_resolves_the_call(self) -> None:
        cid = new_correlation_id()
        for etype in (
            EventType.TOOL_CALL_COMPLETED,
            EventType.TOOL_CALL_FAILED,
            EventType.TOOL_CALL_DENIED,
        ):
            assert (
                find_interrupted_calls(
                    [self._proposed_event(cid), self._outcome_event(cid, etype)]
                )
                == []
            )

    def test_denied_without_proposed_is_not_interrupted(self) -> None:
        # The hook-block path: DENIED alone is proof of non-execution.
        cid = new_correlation_id()
        assert (
            find_interrupted_calls([self._outcome_event(cid, EventType.TOOL_CALL_DENIED)]) == []
        )

    def test_only_the_unclosed_call_is_reported(self) -> None:
        cid = new_correlation_id()
        found = find_interrupted_calls(
            [
                self._proposed_event(cid, call_id="call-open"),
                self._proposed_event(cid, call_id="call-done"),
                self._outcome_event(cid, EventType.TOOL_CALL_COMPLETED, call_id="call-done"),
            ]
        )
        assert [c.call_id for c in found] == ["call-open"]

    def test_malformed_proposed_is_skipped_not_raised(self) -> None:
        cid = new_correlation_id()
        bad = LedgerEvent(
            correlation_id=cid,
            event_type=EventType.TOOL_CALL_PROPOSED,
            payload={"v": 1, "tool": "write_file"},  # no session/call ids
        )
        found = find_interrupted_calls([bad, self._proposed_event(cid)])
        assert [(c.session_id, c.call_id) for c in found] == [("sess-1", "call-1")]


class TestCoderExemption:
    """Coder-only lineages are skipped, never re-queued as tasks."""

    async def test_coder_only_lineage_is_skipped_not_resumed(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.CODER_SESSION_STARTED,
                payload={
                    "v": 1,
                    "session_id": "sess-1",
                    "workspace_path": "/tmp/ws",
                    "permission_mode": "ask",
                },
            )
        )
        await _proposed(ledger, cid, "sess-1", "call-1", "read_file")
        await _outcome(ledger, cid, "sess-1", "call-1", EventType.TOOL_CALL_COMPLETED)

        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_CODER_LINEAGE) == 1
        assert report.count(RecoveryOutcome.RESUMED) == 0
        assert requeuer.calls == []
        assert all(
            e.event_type is not EventType.RECOVERY_REPLAY_COMPLETED
            for e in await ledger.read_correlation(cid)
        )

    async def test_mandate_only_lineage_is_skipped(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        # What `paa agent` leaves behind: one MANDATE_GRANTED on its own
        # lineage per run.
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.MANDATE_GRANTED,
                payload={
                    "v": 1,
                    "session_id": "sess-1",
                    "requester_asserted": {"kind": "coder", "id": "operator"},
                    "identity_assurance": "asserted",
                    "basis": "explicit",
                    "policy_version": "f" * 64,
                    "policy_label": "ask",
                    "scope": {},
                },
            )
        )
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_CODER_LINEAGE) == 1
        assert requeuer.calls == []

    async def test_stale_sweep_row_does_not_flip_a_coder_lineage(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        # A pre-N2.3 sweep could have written RECOVERY_REPLAY_COMPLETED
        # onto a coder lineage; it must still read as coder-only.
        cid = new_correlation_id()
        await _proposed(ledger, cid, "sess-1", "call-1", "read_file")
        await ledger.append(
            LedgerEvent.create(cid, EventType.RECOVERY_REPLAY_COMPLETED, payload={})
        )
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_CODER_LINEAGE) == 1
        assert requeuer.calls == []


class TestInterruptedClosure:
    """The sweep closes unclosed calls, exactly, and only once."""

    async def test_sweep_closes_proposed_without_outcome(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        cid = new_correlation_id()
        await _proposed(
            ledger, cid, "sess-1", "call-1", "write_file",
            pre="b" * 64, post="c" * 64,
        )
        report = await RecoveryEngine(ledger, db).boot_sweep()

        assert report.interrupted_count == 1
        rows = await ledger.read_correlation(cid)
        failed = [r for r in rows if r.event_type is EventType.TOOL_CALL_FAILED]
        assert len(failed) == 1
        assert failed[0].attempt == 0
        assert failed[0].discriminator == "sess-1:call-1:FAILED"
        # Golden v1 FAILED shape: no `state` field inside v1 (ADR-0046).
        assert failed[0].payload == {
            "v": 1,
            "session_id": "sess-1",
            "call_id": "call-1",
            "tool": "write_file",
            "reason": "interrupted",
            "result_sha256": None,
            "args_sha256": args_sha256_of({"path": "note.txt", "content": "after"}),
            "isolation_level": "NONE",
        }
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems

    async def test_second_sweep_closes_nothing_twice(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        cid = new_correlation_id()
        await _proposed(ledger, cid, "sess-1", "call-1", "bash")
        eng = RecoveryEngine(ledger, db)
        first = await eng.boot_sweep()
        count_after_first = await ledger.count()
        second = await eng.boot_sweep()
        assert first.interrupted_count == 1
        assert second.interrupted_count == 0
        assert await ledger.count() == count_after_first

    async def test_completed_calls_are_not_closed_again(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        cid = new_correlation_id()
        await _proposed(ledger, cid, "sess-1", "call-1", "read_file")
        await _outcome(ledger, cid, "sess-1", "call-1", EventType.TOOL_CALL_COMPLETED)
        report = await RecoveryEngine(ledger, db).boot_sweep()
        assert report.interrupted_count == 0
        assert all(
            e.event_type is not EventType.TOOL_CALL_FAILED
            for e in await ledger.read_correlation(cid)
        )

    async def test_mixed_lineage_resumes_and_closes(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        # A task lineage with coder audit interleaved keeps the task
        # path (re-queued) while its interrupted coder call is closed.
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "x"}})
        )
        await ledger.append(
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )
        await _proposed(ledger, cid, "sess-1", "call-1", "bash")

        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
        assert report.interrupted_count == 1
        types = [e.event_type for e in await ledger.read_correlation(cid)]
        assert EventType.TOOL_CALL_FAILED in types


class TestKillSimulation:
    """Close the database mid-lineage, reopen, lose/repeat nothing."""

    @pytest.mark.chaos
    async def test_state_survives_close_and_reopen(self, tmp_path) -> None:
        db_path = tmp_path / "crash.db"
        cid = new_correlation_id()

        db1 = Database(db_path)
        await db1.connect()
        ledger1 = LedgerStore(db1)
        await _proposed(ledger1, cid, "sess-1", "call-keep", "read_file")
        await _outcome(ledger1, cid, "sess-1", "call-keep", EventType.TOOL_CALL_COMPLETED)
        await _proposed(
            ledger1, cid, "sess-1", "call-open", "write_file", pre="b" * 64, post="c" * 64
        )
        await db1.close()  # <-- the "crash": committed rows survive

        db2 = Database(db_path)
        await db2.connect()
        try:
            ledger2 = LedgerStore(db2)
            requeuer = RecordingRequeuer()
            report = await RecoveryEngine(ledger2, db2, requeuer=requeuer).boot_sweep()

            # The completed call stands; exactly the open call was closed.
            assert report.interrupted_count == 1
            assert requeuer.calls == []  # coder-only: never re-queued
            rows = await ledger2.read_correlation(cid)
            assert len([r for r in rows if r.event_type is EventType.TOOL_CALL_FAILED]) == 1
            ok, problems = await ledger2.verify_chain(cid)
            assert ok, problems

            # ... and a second sweep repeats nothing.
            count = await ledger2.count()
            second = await RecoveryEngine(ledger2, db2, requeuer=requeuer).boot_sweep()
            assert second.interrupted_count == 0
            assert await ledger2.count() == count
        finally:
            await db2.close()


class TestSessionRecoveryNote:
    """The resumed model is told, in the transcript, what happened."""

    def test_empty_entries_inject_nothing(self, tmp_path) -> None:
        session = Session("sess-1", transcript_dir=tmp_path)
        assert session.inject_recovery_note([]) is None
        assert session.turns == []

    def test_note_is_a_system_turn_and_idempotent(self, tmp_path) -> None:
        session = Session("sess-1", transcript_dir=tmp_path)
        session.user("fix it")
        first = session.inject_recovery_note(["bash (call c1): unknown — do not re-run"])
        assert first is not None and first.role is TurnRole.SYSTEM
        assert "unknown" in first.content and "D-6" in first.content
        again = session.inject_recovery_note(["bash (call c1): unknown — do not re-run"])
        assert again is first
        assert len([t for t in session.turns if t.role is TurnRole.SYSTEM]) == 1


class _ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, window, tools):
        self.calls += 1
        return ModelTurn(text="done")


class TestResumeRecovery:
    """`--resume` closes interrupted calls and injects the note."""

    async def _seed_interrupted(self, settings, workspace, live: str) -> str:
        """Build agent 1, seed an interrupted write_file, return session id."""
        session = Session(transcript_dir=settings.home / "sessions")
        session.user("fix it")
        session.tool_call("write_file", {"path": "note.txt", "content": "after"})
        agent = await CoderAgent.build(
            settings, session=session, model=_ScriptedModel(), mode=PermissionMode.BYPASS
        )
        sid = session.session_id
        cid = new_correlation_id()
        target = workspace.root / "note.txt"
        target.write_bytes(live.encode("utf-8"))
        args = {"path": "note.txt", "content": "after"}
        await agent.runtime.ledger.append(
            LedgerEvent.create(
                cid,
                EventType.TOOL_CALL_PROPOSED,
                attempt=0,
                discriminator=f"{sid}:call-1:PROPOSED",
                payload={
                    "v": 1,
                    "session_id": sid,
                    "call_id": "call-1",
                    "tool": "write_file",
                    "args_sha256": args_sha256_of(args),
                    "args_preview_redacted": "write_file(...)",
                    "isolation_level": "NONE",
                    "pre_sha256": _sha_text("prior"),
                    "post_sha256": _sha_text("after"),
                    "patch_sha256": None,
                },
            )
        )
        await agent.close()
        return sid

    @pytest.mark.parametrize(
        "live,expected", [("after", "applied"), ("prior", "not_applied"), ("other", "unknown")]
    )
    async def test_resume_classifies_a_file_edit(self, tmp_path, live, expected) -> None:
        home = tmp_path / "paa_home"
        home.mkdir(parents=True)
        settings = Settings(home=home)
        settings.ensure_directories()
        workspace = Workspace(root=tmp_path / "ws")
        workspace.root.mkdir()
        sid = await self._seed_interrupted(settings, workspace, live)

        resumed = Session.resume(sid, settings.home / "sessions")
        agent = await CoderAgent.build(
            settings, session=resumed, model=_ScriptedModel(),
            mode=PermissionMode.BYPASS, workspace=workspace,
        )
        try:
            notes = [t for t in resumed.turns if t.role is TurnRole.SYSTEM]
            assert len(notes) == 1
            assert "write_file" in notes[0].content and expected in notes[0].content
            assert "call-1" in notes[0].content
            # The interrupted call is closed on its own lineage.
            events = await agent.runtime.ledger.events_since(
                datetime(1970, 1, 1, tzinfo=UTC),
                event_types=[EventType.TOOL_CALL_FAILED],
            )
            failed = [e for e in events if e.payload.get("call_id") == "call-1"]
            assert len(failed) == 1
            assert failed[0].payload["reason"] == "interrupted"
        finally:
            await agent.close()

    async def test_resume_reports_bash_as_unknown_and_never_retries(self, tmp_path) -> None:
        home = tmp_path / "paa_home"
        home.mkdir(parents=True)
        settings = Settings(home=home)
        settings.ensure_directories()
        session = Session(transcript_dir=settings.home / "sessions")
        session.user("run tests")
        session.tool_call("bash", {"command": "pytest"})
        agent = await CoderAgent.build(
            settings, session=session, model=_ScriptedModel(), mode=PermissionMode.BYPASS
        )
        sid = session.session_id
        cid = new_correlation_id()
        await agent.runtime.ledger.append(
            LedgerEvent.create(
                cid,
                EventType.TOOL_CALL_PROPOSED,
                attempt=0,
                discriminator=f"{sid}:call-1:PROPOSED",
                payload={
                    "v": 1,
                    "session_id": sid,
                    "call_id": "call-1",
                    "tool": "bash",
                    "args_sha256": args_sha256_of({"command": "pytest"}),
                    "args_preview_redacted": "bash(...)",
                    "isolation_level": "PROCESS",
                    "pre_sha256": None,
                    "post_sha256": None,
                    "patch_sha256": None,
                },
            )
        )
        await agent.close()

        resumed = Session.resume(sid, settings.home / "sessions")
        agent2 = await CoderAgent.build(
            settings, session=resumed, model=_ScriptedModel(), mode=PermissionMode.BYPASS
        )
        try:
            notes = [t for t in resumed.turns if t.role is TurnRole.SYSTEM]
            assert len(notes) == 1
            assert "unknown" in notes[0].content
            assert "do not re-run" in notes[0].content
        finally:
            await agent2.close()
