"""``paa thread`` — the local-first control plane (SPEC 2 Wave 7, C7.5).

One coding thread across harnesses. A **thread** is the thing that survives
a harness swap: the mandate (who asked for this work, under which policy),
the context handoff packet (what the next harness is handed when it takes
over), the workspace the work happens in, and the ordered segments — one
per harness stint. PAA holds the thread; harnesses are hands.

The package's own map:

* :mod:`paa.thread.record` — :class:`ThreadRecord` / :class:`Segment`, the
  durable per-thread JSON under ``PAA_HOME/threads/`` and the append-only
  invariants its methods enforce.
* :mod:`paa.thread.journal` — :class:`ThreadJournal`, the ADR-0046-style
  emitter that appends the four thread event types to the ledger under the
  thread's correlation id.
* :mod:`paa.thread.handoff` — :class:`HandoffPacket` and
  :func:`assemble_handoff`: mandate + gathered memory + ledger tail + git
  summary, canonically serialised and hashed, so "context intact" is a
  checkable fact rather than a claim.
* :mod:`paa.thread.harness` — :class:`HarnessBinding` and the registry
  where real harnesses (Claude Code, Codex, Cursor, OpenCode) plug in;
  two fake bindings ship for offline, deterministic runs.
* :mod:`paa.thread.engine` — :class:`ThreadEngine`, the lifecycle:
  create, run a segment, swap the harness mid-task, list and show.

Two honesty lines this package states once, in the place the code enforces
them:

**The thread file is the fast handle, the ledger is the truth.** The
thread's lifecycle is journalled as ledger events under the thread id (the
correlation id) *before* the effects they describe; ``<id>.json`` is a
projection for ``paa thread show``, rewriteable the way the recorder's
summary is, never the record itself.

**No rollback promises.** A segment that crashed is closed as
``interrupted`` and the thread moves on; nothing here reverts the
workspace. The evidence of what happened is the chain, not a restore.
"""

from paa.thread.engine import SegmentOutcome, ThreadEngine, create_worktree
from paa.thread.errors import ThreadError
from paa.thread.handoff import HandoffPacket, assemble_handoff, collect_git_summary
from paa.thread.harness import BindingContext, HarnessBinding, get_binding, known_harnesses
from paa.thread.journal import ThreadJournal
from paa.thread.record import (
    Segment,
    ThreadRecord,
    ThreadStatus,
    load_thread,
    save_thread,
    thread_path,
)

__all__ = [
    "BindingContext",
    "HandoffPacket",
    "HarnessBinding",
    "Segment",
    "SegmentOutcome",
    "ThreadEngine",
    "ThreadError",
    "ThreadJournal",
    "ThreadRecord",
    "ThreadStatus",
    "assemble_handoff",
    "collect_git_summary",
    "create_worktree",
    "get_binding",
    "known_harnesses",
    "load_thread",
    "save_thread",
    "thread_path",
]
