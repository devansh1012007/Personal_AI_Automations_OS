# W7-IMPL-06 — C7.5: `paa thread`, the local-first control plane

**Task:** SPEC 2 §6 row C7.5 — one coding thread across harnesses; swap
harness mid-thread; resume anywhere; the ledger unbroken.
**Status:** landed. The product story — PAA holds the thread, harnesses are
hands — has its first executable form.

## What landed

* `src/paa/thread/` — the package:
  * `record.py` — `ThreadRecord` / `Segment`: durable JSON under
    `PAA_HOME/threads/<id>.json`, written atomically, append-only
    invariants enforced by the methods rather than by convention.
  * `journal.py` — `ThreadJournal`: the ADR-0046-style emitter appending
    the four new event types under the thread's correlation id.
  * `handoff.py` — `HandoffPacket` / `assemble_handoff`: mandate + gathered
    memory + ledger tail + git summary, canonically serialised and hashed —
    "context intact" is a checkable fact, not a claim.
  * `harness.py` — `HarnessBinding` + the registry where real harnesses
    (Claude Code, Codex, Cursor, OpenCode) plug in; `fake-alpha` and
    `fake-beta` ship for offline deterministic runs.
  * `engine.py` — `ThreadEngine`: create, run a segment, swap mid-task,
    list, show.
  * `errors.py`, `fixtures/` (the two fake harness scripts).
* Ledger vocabulary (append-only, ADR-0046 pattern): `THREAD_CREATED`,
  `THREAD_SEGMENT_STARTED` (carries `packet_sha256` — the content address
  of the handoff the segment started from), `THREAD_SEGMENT_ENDED`
  (carries the harness's own fate), `THREAD_HARNESS_SWAPPED`.
  `ledger/replay.py` folds them without requeueing them as tasks;
  `ledger/recovery.py` taught the boot sweep the same.
* `src/paa/cli/thread.py` — `paa thread new|run|swap|show|list`;
  registered in `cli/main.py`.
* `tests/thread/` — 99 tests: record persistence round-trip, registry,
  handoff assembly + hash stability, the **swap acceptance** (start on
  fake-alpha, swap to fake-beta, assert same thread id and mandate, second
  packet contains the first segment's effects, packet-hash chain intact,
  ledger chain ordered and unbroken), event payload goldens, replay
  property, CLI smoke.

## Design decisions

* **The thread file is the fast handle, the ledger is the truth.**
  Lifecycle events are journalled *before* the effects they describe;
  `<id>.json` is a projection for `paa thread show`, never the record.
* **The handoff packet is content-addressed.** Each segment records the
  sha256 of the packet it started from, so a swap's "context intact" is
  verified by comparing content addresses, not by trusting a flag.

## Gates

`pytest tests/thread` 99 passed; `tests/ledger` 161 green with the new
vocabulary; `tests/conventions` 34 green; ruff 0 and mypy 0 on the thread
package, the CLI, `core/types.py` and the two edited ledger modules.

## Honest residuals

The two shipped bindings are fakes; real harness binaries arrive through
the same registry. Workspace handling is v1: caller-provided path, with
git-worktree creation offered but not promised (no rollback story — that
is Wave 9's effect-registry work, not this wave's claim).
