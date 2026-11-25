"""The handoff packet — what a new harness is handed when it takes over.

The spec's sentence is *"swap harness mid-thread, continue with context
intact"* — and "intact" is only a claim until it is checkable. The packet
is how it becomes checkable: the context a segment starts from is
assembled once, serialised canonically (sorted keys, no whitespace —
:func:`paa.ledger.events.canonical_json`, the same function the ledger's
own hash chain trusts) and hashed, and the hash is recorded on the
segment. A later reader recomputes the hash from the persisted packet and
compares it to the segment's row: equal means the packet on disk is the
packet that stint started from, byte for byte.

Four inputs, one per source the spec names:

* **the mandate** — the thread's serialized v1 grant, verbatim: who asked,
  under which policy, over what scope;
* **memory** — a :class:`~paa.context.gatherer.ContextPacket` compiled by
  the :class:`~paa.context.gatherer.BoundedContextGatherer`, so the
  bounded-context machinery (dedup, ranking, the hard token ceiling)
  applies to the handoff exactly as it applies to a planner's context.
  The thread's own objective is injected as the packet's first-class
  element (slot ``objective``); recalled memory facts join it as ordinary
  candidates;
* **the ledger tail** — the thread lineage's most recent rows, as
  *identity* rows (``state_version``, ``event_type``, ``event_hash``).
  Identity, not payload: an event's hash already commits to its content,
  so listing the hash is cryptographically stronger than quoting the body,
  and it keeps the packet bounded no matter how large the payloads were;
* **the git summary** — status and recent commits of the workspace the
  thread runs in, so the incoming harness sees the shape of the tree it
  inherits (bounded to a fixed line count; a dirty line beyond the cap is
  counted, not silently dropped).

Pure by construction: :func:`assemble_handoff` records no timestamp of
its own (the ``THREAD_SEGMENT_STARTED`` row already timestamps the
handoff, and duplicating it here would break same-inputs-same-hash for
nothing), so the packet's hash is a function of its inputs and nothing
else.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.context.gatherer import BoundedContextGatherer, ContextElement
from paa.core.errors import SandboxError
from paa.ledger.events import LedgerEvent, canonical_json
from paa.sandbox.base import Sandbox
from paa.thread.errors import ThreadError
from paa.thread.record import ThreadRecord

__all__ = [
    "HANDOFF_SCHEMA",
    "HandoffPacket",
    "assemble_handoff",
    "collect_git_summary",
    "packet_path",
    "read_packet",
    "save_packet",
]

#: The handoff packet's identity tag. Additive changes only within ``/1``.
HANDOFF_SCHEMA = "paa-handoff/1"

#: How many ledger rows ride in the tail. The newest rows are the ones a
#: handover needs (what just happened); older history is the ledger's job
#: to serve, not the packet's.
LEDGER_TAIL_ROWS: int = 25

#: How many git status / log lines ride in the summary, each. Beyond the
#: cap the counts are still reported (``status_truncated`` and friends),
#: so the incoming harness knows the tree is dirtier than it can see.
GIT_SUMMARY_LINES: int = 20

#: The required slot every thread packet demands: its own objective. The
#: gatherer treats a resolved required slot as first-tier packing, so the
#: objective survives any amount of recalled-memory noise — which is the
#: point of handing it over.
OBJECTIVE_SLOT = "objective"


@dataclass(frozen=True, slots=True)
class HandoffPacket:
    """The assembled handoff: mandate, gathered memory, ledger tail, git.

    ``ledger_tail`` is a tuple of identity rows, oldest first; ``memory``
    is the compiled :class:`~paa.context.gatherer.ContextPacket` as plain
    JSON; ``git`` is the summary dict :func:`collect_git_summary` shapes.
    ``mandate`` is the thread's grant payload verbatim.
    """

    thread_id: str
    segment_id: str
    mandate: dict[str, Any]
    memory: dict[str, Any]
    ledger_tail: tuple[dict[str, Any], ...]
    git: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        """The packet's persisted form — exactly the fields hashed."""
        return {
            "schema": HANDOFF_SCHEMA,
            "thread_id": self.thread_id,
            "segment_id": self.segment_id,
            "mandate": self.mandate,
            "memory": self.memory,
            "ledger_tail": [dict(row) for row in self.ledger_tail],
            "git": self.git,
        }

    def canonical_bytes(self) -> bytes:
        """The bytes the hash commits to. Sorted keys, no whitespace."""
        return canonical_json(self.to_json()).encode("utf-8")

    def sha256(self) -> str:
        """The packet's content address, as recorded on the segment."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _objective_element(thread: ThreadRecord) -> ContextElement:
    """The thread's own objective, as the packet's first-class element.

    The mandate's label rides in the content because a harness that knows
    which policy posture it inherited is a harness that can refuse work
    the posture forbids; the id and slot make the element dedupable and
    first-tier in the gatherer's packing (it resolves ``objective``).
    """
    label = thread.mandate.get("policy_label")
    policy = f" under policy {label}" if isinstance(label, str) and label else ""
    return ContextElement(
        id=f"thread:{thread.id}:mandate",
        content=f"objective: {thread.title}{policy}",
        relevance=1.0,
        confidence=1.0,
        importance=1.0,
        slot=OBJECTIVE_SLOT,
        memory_domain="thread",
    )


def _identity_rows(events: Sequence[LedgerEvent]) -> tuple[dict[str, Any], ...]:
    """The tail's identity rows: newest ``LEDGER_TAIL_ROWS``, oldest first.

    An event with no ``event_hash`` was never sealed (it did not come from
    the store); the row says so with ``None`` rather than being dropped —
    a gap the reader can see is honest, a silently shortened tail is not.
    """
    tail = list(events[-LEDGER_TAIL_ROWS:])
    return tuple(
        {
            "state_version": event.state_version,
            "event_type": event.event_type.value,
            "event_hash": event.event_hash,
        }
        for event in tail
    )


def assemble_handoff(
    *,
    thread: ThreadRecord,
    segment_id: str,
    gatherer: BoundedContextGatherer,
    memory_elements: Sequence[ContextElement] = (),
    ledger_tail: Sequence[LedgerEvent] = (),
    git_summary: Mapping[str, Any] | None = None,
) -> HandoffPacket:
    """Assemble one segment's handoff packet. Pure: no I/O, no clock.

    ``memory_elements`` are the *already retrieved* memory candidates (the
    gatherer performs no retrieval of its own — its contract); the
    objective element joins them as a hot fact, so the mandate always
    reaches the incoming harness through the same bounded channel the
    recalled facts do. ``git_summary`` may be ``None`` — a workspace with
    no git story is a fact the packet states (``available: False``), not
    an error to hide.
    """
    memory_packet = gatherer.compile(
        (_objective_element(thread), *memory_elements),
        (),
        (OBJECTIVE_SLOT,),
    )
    return HandoffPacket(
        thread_id=thread.id,
        segment_id=segment_id,
        mandate=dict(thread.mandate),
        memory=memory_packet.model_dump(mode="json"),
        ledger_tail=_identity_rows(ledger_tail),
        git=dict(git_summary) if git_summary is not None else {"available": False},
    )


# ---------------------------------------------------------------------------
# The git half — through the sandbox, like every git question this tree asks
# ---------------------------------------------------------------------------


def _bounded_lines(text: str) -> tuple[list[str], bool]:
    """Non-empty lines capped at :data:`GIT_SUMMARY_LINES`, plus a cut flag."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[:GIT_SUMMARY_LINES], len(lines) > GIT_SUMMARY_LINES


async def collect_git_summary(sandbox: Sandbox, workspace: Path) -> dict[str, Any]:
    """The workspace's git story: inside, HEAD, status, recent commits.

    Asked through :func:`paa.observe.observer.git_query` — the one git
    vocabulary the observer and the recorder already share — so the thread
    cannot drift into a second way of asking git what it sees. A host
    without git, or a workspace that is not a worktree, is recorded as
    such (``available: False`` with the reason); git is an optional
    witness, not a prerequisite for a thread.
    """
    from paa.observe.observer import git_query

    try:
        inside = await git_query(sandbox, workspace, ("rev-parse", "--is-inside-work-tree"))
    except SandboxError:
        return {"available": False, "reason": "git unavailable in sandbox"}
    if inside.exit_code != 0:
        return {"available": False, "reason": "not a git worktree"}

    head: str | None = None
    resolved = await git_query(sandbox, workspace, ("rev-parse", "HEAD"))
    if resolved.exit_code == 0:
        head = resolved.stdout.strip() or None

    status_lines, status_cut = _bounded_lines(
        (await git_query(sandbox, workspace, ("status", "--porcelain=v1"))).stdout
    )

    commits: list[str] = []
    commits_cut = False
    if head is not None:
        # ``--end-of-options`` closes the option-injection door the attest
        # verifier already closed elsewhere: a ref that starts with ``-``
        # must be read as a ref, never as a flag. ``head`` here is git's
        # own output, but the habit costs one word and removes the class.
        log = await git_query(
            sandbox,
            workspace,
            ("log", "--oneline", "-n", str(GIT_SUMMARY_LINES), "--end-of-options", "HEAD"),
        )
        if log.exit_code == 0:
            commits, commits_cut = _bounded_lines(log.stdout)

    return {
        "available": True,
        "head": head,
        "dirty": bool(status_lines),
        "status": status_lines,
        "status_truncated": status_cut,
        "recent_commits": commits,
        "commits_truncated": commits_cut,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def packet_path(home: Path, thread_id: str, segment_id: str) -> Path:
    """Where one segment's handoff packet is persisted."""
    return Path(home) / "threads" / "packets" / thread_id / f"{segment_id}.json"


def save_packet(home: Path, packet: HandoffPacket) -> Path:
    """Write one handoff packet atomically; returns the path written."""
    path = packet_path(home, packet.thread_id, packet.segment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(packet.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)
    return path


def read_packet(path: Path) -> HandoffPacket:
    """Read one handoff packet; refuses absent and foreign files.

    The tag is the identity: a JSON file that is not a ``paa-handoff/1``
    packet is refused, not reinterpreted. The returned packet's
    :meth:`HandoffPacket.sha256` recomputes over the parsed fields, so the
    caller's comparison against the segment's recorded hash is a real
    check of the bytes' content, not a copy of the stored digest.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ThreadError(f"no readable handoff packet at {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != HANDOFF_SCHEMA:
        found = raw.get("schema") if isinstance(raw, dict) else type(raw).__name__
        raise ThreadError(
            f"{path} is not a {HANDOFF_SCHEMA} handoff packet", schema=found
        )
    return HandoffPacket(
        thread_id=str(raw["thread_id"]),
        segment_id=str(raw["segment_id"]),
        mandate=dict(raw["mandate"]),
        memory=dict(raw["memory"]),
        ledger_tail=tuple(dict(row) for row in raw["ledger_tail"]),
        git=dict(raw["git"]),
    )
