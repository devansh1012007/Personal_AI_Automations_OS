"""The thread engine — the control plane's lifecycle (C7.5).

One class holds the sequencing, and the sequencing is the whole design:

* **create** — mint the id (which *is* the ledger correlation id), grant
  the mandate, journal ``MANDATE_GRANTED`` then ``THREAD_CREATED``, and
  only then write the thread file. Ledger before effect, in that order,
  for the same reason it is the house rule everywhere else: a crash
  between the events and the file leaves a lineage that says a thread was
  born, which is true and recoverable, rather than a file no lineage
  vouches for.
* **run a segment** — close anything a crash left dangling (as
  ``interrupted``, honestly), assemble the handoff packet, persist it,
  journal ``THREAD_SEGMENT_STARTED`` **before the harness spawns**, run
  the bound command, journal ``THREAD_SEGMENT_ENDED``, close the segment.
* **swap** — journal ``THREAD_HARNESS_SWAPPED`` between the stints, then
  run under the new binding. The swap is three ledger rows (ended,
  swapped, started), which is what makes it a *transition* in the record
  rather than two unrelated runs.

Two containment stories, mirrored from the recorder and never collapsed:
``--record`` runs the harness through
:class:`paa.recorder.core.RecorderCore` — a real
:class:`~paa.sandbox.base.SandboxSession` held for the harness's whole
life, the hash-chained flight record, the observe bundle, the proxy door
open for mediated tool calls — and the segment carries the run directory
as its evidence pointer. Without ``--record`` the harness still runs
contained (the sandbox's one-shot contract: jailed cwd, allowlisted env,
wall clock, memory ceiling) but the segment's evidence is the packet, the
ledger and the caller's own eyes — the T1 floor, stated as such.

No rollback is attempted or implied anywhere in this module. A crashed
segment is *closed as interrupted*; the workspace it half-wrote is the
operator's to inspect with the ledger and the recorder's artifacts, not
something this engine quietly reverts.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from paa.config import PolicySettings
from paa.context.gatherer import BoundedContextGatherer, ContextElement
from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.sandbox.base import Sandbox, SandboxSpec
from paa.thread.errors import ThreadError
from paa.thread.handoff import HandoffPacket, assemble_handoff, collect_git_summary, save_packet
from paa.thread.harness import BindingContext, get_binding, known_harnesses
from paa.thread.journal import ThreadJournal
from paa.thread.record import (
    Segment,
    ThreadRecord,
    ThreadStatus,
    list_threads,
    load_thread,
    save_thread,
)

__all__ = [
    "SegmentOutcome",
    "ThreadEngine",
    "ThreadMemorySource",
    "create_worktree",
]


class ThreadMemorySource(Protocol):
    """Where a segment's recalled-memory candidates come from.

    The gatherer performs no retrieval of its own (its contract), so the
    engine asks this narrow protocol instead: given the thread, the
    candidates. The CLI wires an adapter over the runtime's fact store;
    tests wire synthetic elements; ``None`` (the engine default) means no
    memory half at all — an honest packet with an empty memory block, not
    a fabricated one.
    """

    async def elements(self, thread: ThreadRecord) -> list[ContextElement]: ...


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    """Everything one run segment produced, for the CLI to render."""

    thread: ThreadRecord
    segment: Segment
    packet: HandoffPacket
    packet_path: Path
    exit_code: int | None
    timed_out: bool
    duration_ms: float
    record_run_dir: Path | None
    stdout: str
    stderr: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


async def create_worktree(target: Path, *, branch: str, path: Path) -> Path:
    """Add a git worktree at ``path`` (branch ``branch``) to ``target``.

    Host-side setup, run directly rather than through the sandbox: this is
    the control plane arranging the workspace, not a harness touching it —
    the same split ``paa record`` makes when it snapshots git before the
    child spawns. ``git`` is exec'd with a fixed argv (never a shell), and
    both failure modes are refused with their own stderr: a target that is
    not a repository (``--worktree`` has nothing to add to) and a
    ``worktree add`` that failed for git's own reasons (branch exists,
    path occupied, …). No partial-state cleanup is attempted — a failed
    ``worktree add`` leaves nothing this function promised.
    """
    probe = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(target),
        "rev-parse",
        "--is-inside-work-tree",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await probe.communicate()
    if probe.returncode != 0 or out.strip() != b"true":
        raise ThreadError(
            f"{target} is not a git repository — --worktree needs one to add to",
            target=str(target),
        )
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(target),
        "worktree",
        "add",
        "-b",
        branch,
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise ThreadError(
            "git worktree add failed",
            branch=branch,
            path=str(path),
            stderr=err.decode("utf-8", errors="replace")[-400:],
        )
    return path


class ThreadEngine:
    """The lifecycle: create, run a segment, swap, list, show.

    Stateless between calls — every method loads the thread from disk,
    does its work, and writes it back atomically — so two processes (or a
    crashed one and its replacement) cannot corrupt each other through
    shared in-memory state. The ledger remains the serialised truth.
    """

    def __init__(
        self,
        *,
        ledger: LedgerStore,
        home: Path,
        sandbox: Sandbox,
        gatherer: BoundedContextGatherer | None = None,
        memory_source: ThreadMemorySource | None = None,
    ) -> None:
        self._ledger = ledger
        self._home = Path(home)
        self._sandbox = sandbox
        self._gatherer = gatherer if gatherer is not None else BoundedContextGatherer()
        self._memory_source = memory_source

    # -- reads ------------------------------------------------------------

    def load(self, thread_id: str) -> ThreadRecord:
        """One thread's record, or the refusal naming what was asked for."""
        return load_thread(self._home, thread_id)

    def list_threads(self) -> list[ThreadRecord]:
        """Every thread, oldest-created first (the record module's order).

        Named ``list_threads``, not ``list``: a method that shadows the
        ``list`` builtin inside the class body turns every later
        ``list[...]`` annotation in this class into a type error, and the
        readable name is the cheaper fix.
        """
        return list_threads(self._home)

    # -- create -----------------------------------------------------------

    async def create_thread(
        self,
        *,
        title: str,
        workspace: Path,
        policy: PolicySettings,
        requester_id: str = "operator",
        default_harness: str | None = None,
        worktree: bool = False,
    ) -> ThreadRecord:
        """Create a thread: mandate, ledger rows, then the file.

        The operator at the CLI is the explicit requester (the ``paa
        agent`` rule, applied to the control plane): the *server* stamps
        the effective policy version, so the thread cannot carry a policy
        its grant never saw. ``worktree=True`` re-points the thread's
        workspace at a fresh ``git worktree`` of the given repository,
        created under ``<home>/threads/worktrees/<id>`` on its own branch
        — the repo itself stays clean, and the thread's tree is disposable
        without touching the operator's checkout.
        """
        if not title.strip():
            raise ThreadError("a thread needs a non-empty title")
        if default_harness is not None:
            get_binding(default_harness)  # refuse unknown names at creation
        target = workspace.expanduser().resolve()
        if not worktree and not target.is_dir():
            raise ThreadError(
                "the workspace must be an existing directory",
                workspace=str(target),
            )

        thread_id = uuid.uuid4()
        if worktree:
            wt_path = self._home / "threads" / "worktrees" / str(thread_id)
            target = await create_worktree(
                target, branch=f"paa-thread/{thread_id}", path=wt_path
            )

        from paa.ledger.mandate import explicit_mandate

        mandate = explicit_mandate(
            kind="thread",
            requester_id=requester_id,
            policy=policy,
            workspace=str(target),
        )
        now = _now_iso()
        record = ThreadRecord(
            id=str(thread_id),
            title=title,
            workspace=str(target),
            created_at=now,
            updated_at=now,
            mandate=mandate.granted_payload(session_id=None),
            default_harness=default_harness,
        )

        # Ledger before effect: the grant, then the birth, then the file.
        # The mandate event is separate from THREAD_CREATED on purpose —
        # `paa ledger <id>` renders it through the mandate summary, and
        # `paa doctor`'s basis counts read it, exactly as they do for a
        # coder session's grant.
        await self._ledger.append(
            LedgerEvent.create(
                thread_id, EventType.MANDATE_GRANTED, payload=record.mandate
            )
        )
        await ThreadJournal(self._ledger, thread_id).created(
            title=title,
            workspace=record.workspace,
            status=ThreadStatus.ACTIVE.value,
        )
        save_thread(self._home, record)
        return record

    # -- run + swap ---------------------------------------------------------

    async def run_segment(
        self,
        thread_id: str,
        *,
        harness: str | None = None,
        record: bool = False,
        timeout_s: float = 600.0,
        memory_mb: int = 512,
        quiet: bool = False,
    ) -> SegmentOutcome:
        """Open a segment, hand off, run the harness, close the segment."""
        thread = self.load(thread_id)
        self._refuse_closed(thread)
        harness_name = self._resolve_harness(thread, harness)
        binding = get_binding(harness_name)
        await self._close_dangling(thread)

        segment_id = uuid.uuid4().hex
        workspace = Path(thread.workspace)

        # The handoff is assembled and persisted before the segment event,
        # because the event carries the packet's hash: the row on the chain
        # commits to bytes that already exist on disk.
        memory_elements = await self._memory_for(thread)
        ledger_tail = await self._ledger.read_correlation(thread.correlation_id())
        git_summary = await collect_git_summary(self._sandbox, workspace)
        packet = assemble_handoff(
            thread=thread,
            segment_id=segment_id,
            gatherer=self._gatherer,
            memory_elements=memory_elements,
            ledger_tail=ledger_tail,
            git_summary=git_summary,
        )
        packet_file = save_packet(self._home, packet)

        journal = ThreadJournal(self._ledger, thread.correlation_id())
        await journal.segment_started(
            segment_id,
            harness=harness_name,
            packet_sha256=packet.sha256(),
            workspace=thread.workspace,
        )
        segment = Segment(
            segment_id=segment_id,
            harness=harness_name,
            started_at=_now_iso(),
            packet_sha256=packet.sha256(),
            packet_path=str(packet_file),
        )
        thread.open_segment(segment)
        save_thread(self._home, thread)

        context = BindingContext(
            workspace=workspace, thread_id=thread.id, segment_id=segment_id
        )
        argv = binding.command_for(context)
        env = binding.env_for(context)

        if record:
            # The mediated story: the recorder's whole evidence boundary —
            # a live SandboxSession, the flight record, the observe bundle,
            # the proxy door for tool calls — with the run directory as
            # the segment's evidence pointer.
            from paa.recorder.core import RecorderCore

            core = RecorderCore(self._sandbox, home=self._home)
            outcome = await core.record(
                argv,
                workspace=workspace,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
                child_env=env,
                goal=thread.title,
                attest=False,
                quiet=quiet,
            )
            result = outcome.result
            run_dir: Path | None = outcome.run_dir
        else:
            # The plainly contained story: jailed cwd, allowlisted env,
            # wall clock, memory ceiling — and no evidence beyond the
            # packet, the ledger and the stdout relayed after the fact.
            result = await self._sandbox.run(
                SandboxSpec(
                    command=argv,
                    workspace_path=workspace,
                    env=env,
                    timeout_seconds=timeout_s,
                    memory_mb=memory_mb,
                )
            )
            run_dir = None

        await journal.segment_ended(
            segment_id,
            harness=harness_name,
            reason="completed",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            record_run_dir=str(run_dir) if run_dir is not None else None,
        )
        thread.close_segment(
            segment_id,
            ended_at=_now_iso(),
            reason="completed",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            record_run_dir=str(run_dir) if run_dir is not None else None,
        )
        save_thread(self._home, thread)
        return SegmentOutcome(
            thread=thread,
            segment=thread.segments[-1],
            packet=packet,
            packet_path=packet_file,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
            record_run_dir=run_dir,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    async def swap(
        self,
        thread_id: str,
        *,
        harness: str,
        record: bool = False,
        timeout_s: float = 600.0,
        memory_mb: int = 512,
        quiet: bool = False,
    ) -> SegmentOutcome:
        """Sugar for the demo verb: record the transition, then run.

        The swap row sits between the stints by construction — the
        previous segment is already ended (or is closed as interrupted
        here), this row names the hand-over, and the new segment's STARTED
        row follows it. Swapping to the binding already in use is allowed
        and recorded as what it is: a fresh stint under the same hand.
        """
        thread = self.load(thread_id)
        self._refuse_closed(thread)
        if not thread.segments:
            raise ThreadError(
                "nothing to swap from — run a segment on the thread first",
                thread_id=thread_id,
            )
        get_binding(harness)
        last = thread.segments[-1]
        from_harness = last.harness
        await self._close_dangling(thread)
        await ThreadJournal(self._ledger, thread.correlation_id()).harness_swapped(
            from_harness=from_harness,
            to_harness=harness,
            after_segment=last.segment_id,
        )
        return await self.run_segment(
            thread_id,
            harness=harness,
            record=record,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            quiet=quiet,
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _refuse_closed(thread: ThreadRecord) -> None:
        if thread.status is ThreadStatus.CLOSED:
            raise ThreadError(
                "the thread is closed — no further segments",
                thread_id=thread.id,
            )

    @staticmethod
    def _resolve_harness(thread: ThreadRecord, harness: str | None) -> str:
        name = harness if harness is not None else thread.default_harness
        if name is None:
            raise ThreadError(
                "no harness to run: pass --harness, or create the thread with one",
                thread_id=thread.id,
                known=known_harnesses(),
            )
        return name

    async def _close_dangling(self, thread: ThreadRecord) -> None:
        """Close a segment a crash left open, as ``interrupted``.

        The duration is computed from the segment's own ``started_at``
        (floor zero — a clock that went backwards is a host fact, not a
        negative runtime), and the close rides the same ledger row + file
        update as an ordinary close. There is no sweep half to this in
        v1: a thread that is never touched again keeps its open segment
        visible in ``paa thread show``, which is the honest alternative to
        a boot-time guess.
        """
        dangling = thread.last_open_segment()
        if dangling is None:
            return
        started = datetime.fromisoformat(dangling.started_at)
        duration_ms = max(0.0, (datetime.now(UTC) - started).total_seconds() * 1000)
        await ThreadJournal(self._ledger, thread.correlation_id()).segment_ended(
            dangling.segment_id,
            harness=dangling.harness,
            reason="interrupted",
            exit_code=None,
            timed_out=False,
            duration_ms=duration_ms,
            record_run_dir=None,
        )
        thread.close_segment(
            dangling.segment_id,
            ended_at=_now_iso(),
            reason="interrupted",
            exit_code=None,
            timed_out=False,
            duration_ms=duration_ms,
            record_run_dir=None,
        )
        save_thread(self._home, thread)

    async def _memory_for(self, thread: ThreadRecord) -> list[ContextElement]:
        if self._memory_source is None:
            return []
        return list(await self._memory_source.elements(thread))
