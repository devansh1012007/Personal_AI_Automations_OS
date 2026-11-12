<!-- ORCH-DIGEST:START -->
agent: W2-IMPR-05 | scope: Wave 2c improve of W2-REV-04 MAJOR-1 + MINOR-1/MINOR-2 | verdict: DONE-WITH-NEED
findings: MAJOR 1 fixed | MINOR 3 covered (escalation shape tests) | NIT 0
claims_audit: review §4 items addressed; escalation-shape tests added
band_aids_found: none
top_3_risks: conventions 33/34 pending ORCH docs sweep (NEED filed)
optimizations: DO-NOW 1 (fold MODEL_ESCALATED) | DO-LATER 1 deferred (session-scoped query)
reversal_check: red->green verified (escalated/orphan RESUMED pre-fix, SKIPPED post-fix)
confidence: high (root cause reproduced by review probes P1/P3, mirrored by red tests)
<!-- ORCH-DIGEST:END -->
# Improve — W2-IMPR-05 (W2-REV-04 MAJOR-1 + MINOR-1 + MINOR-2)

## Context
W2-REV-04 §4 MAJOR-1: `MODEL_ESCALATED` is `_NO_PHASE_CHANGE` (ledger/replay.py:412)
but absent from `_CODER_EVENT_TYPES` (recovery.py:264-275), so any coder lineage
that escalated — or an orphan `MODEL_ESCALATED` (models/router.py:831-841, the
`correlation_id=None` CLI path) — fails the subset test at recovery.py:662 and
is RESUMED + requeued as a phantom task. Task lineages still carry TASK_*/
EXECUTION_* events, so adding MODEL_ESCALATED to the set cannot over-exempt them.

MINOR-1: `find_interrupted_calls_for_session` (recovery.py:416-446) scans
`events_since` (store.py:362-378, `ORDER BY sequence_id ASC LIMIT ?`) and
filters by session after the cut, so the bound evicts the newest work; its
docstring wrongly claims newest-first protection.
MINOR-2: `_resume_entry` (coder/agent.py:312-358) sets `live_checked=True` only
when the jailed target exists, so an absent creation file classifies `unknown`
instead of `not_applied` even though `classify_interrupted` (recovery.py:340-347)
supports `(pre=None, live=None) -> NOT_APPLIED`.

## Phases executed
### Phase 1 — MAJOR-1 (code 357134a)
`src/paa/ledger/recovery.py`: added `EventType.MODEL_ESCALATED` to
`_CODER_EVENT_TYPES` with the reasoning in the set's comment (phase-neutral
audit row, never stand-alone task work; task lineages still fail the subset
test). Regression tests in `tests/coder/test_recovery_chaos.py` (`TestCoderExemption`):
escalated coder lineage → `SKIPPED_CODER_LINEAGE`, orphan `MODEL_ESCALATED` →
skipped, task lineage + escalation → still `RESUMED` + requeued (over-exemption
guard). Red first: both exemption tests failed pre-fix with `outcomes={'RESUMED': 1}`
(exactly the review's P1/P3 probes); the task-lineage guard passed throughout.

### Phase 2 — MINOR-1 (same commit)
`src/paa/ledger/store.py::events_since`: new keyword `descending: bool = False`
(fixed-literal branches, no interpolated SQL); default preserves ASC for the
reflection/CLI counting callers. `find_interrupted_calls_for_session` now passes
`descending=True`, reverses to chronological order (identical output order when
under the limit), and its docstring now states the newest-first behavior instead
of the false claim. Tests: `TestFindInterruptedCallsForSession.test_bound_evicts_oldest_not_newest`
(limit=2, target is the newest tool event — fails on ascending scan) and
`test_results_keep_chronological_order` (pins the reverse).

### Phase 3 — MINOR-2 (same commit)
`src/paa/coder/agent.py::_resume_entry`: jailed target present-but-absent
(`resolved is not None and not is_file()`) → `live_checked=True, live_sha256=None`,
delegating the verdict to `classify_interrupted` (creation → `NOT_APPLIED`, edit
whose file vanished → `UNKNOWN`). Unreadable file (`OSError`) still degrades to
`UNKNOWN`; jail violation / no workspace still `UNKNOWN`. Test:
`test_resume_absent_creation_is_not_applied` (note reads `not_applied` +
`safe to retry`, no `do not re-run`).

## Long-run test
- **Source:** exemption stays a pure subset test over persisted event types;
  no new state, no replay change. Scan change is query-local (ordering only);
  `find_interrupted_calls` is order-independent.
- **Load (100x/concurrency/partial failure):** per-lineage cost unchanged
  (one read + existing closes); scan still O(limit) rows. Crash mid-sweep
  re-closes idempotently; escalation rows can no longer mint per-boot phantom
  tasks.
- **Hostile input:** malformed payloads unchanged (fold skips them); the new
  `descending` flag is a bool → both ORDER BY literals are fixed strings, no
  interpolation surface. Absent-file verdict requires jailed path resolution
  first (`assert_inside_workspace`).
- **Operability:** same outcomes vocabulary; `SKIPPED_CODER_LINEAGE` count now
  truthful for escalated/orphan lineages. Rollback = revert 357134a (pre-fix
  lineages resume as tasks again — the bug, not corruption).
- **Reversal:** reverting the `_CODER_EVENT_TYPES` line fails the two exemption
  tests (RESUMED, requeued=1); reverting `descending=True` fails
  `test_bound_evicts_oldest_not_newest`; reverting the `elif` fails
  `test_resume_absent_creation_is_not_applied`. All verified red pre-fix.

## Review audit
- Self-review per phase: subset semantics re-checked (empty/pure-neutral
  histories still non-exempt via `bool(kinds)`); `resolved`-is-`None` (jail
  failure) still degrades to `UNKNOWN`; pre-existing callers of `events_since`
  untouched (default ASC). No band-aids, no suppressions added.
- `src/paa/coder/agent.py` also carries a sibling N2.6 blobbing-journal hunk
  (uncommitted in the shared worktree, lines ~178-237) — committed as-is per
  standing orders Rule 12; my hunks are the `_resume_entry` ones only. The
  scoped suite passes on the combined tree.
- No independent reviewer subagent was available to spawn (same-session tools);
  self-review recorded here.

## Verification evidence
- `.venv/Scripts/python -m pytest tests/coder/test_recovery_chaos.py tests/ledger/test_recovery.py -q` → 65 passed, exit 0 (59 pre-existing + 6 new).
- Pre-fix red run of the two MAJOR-1 exemption tests → `outcomes={'RESUMED': 1}` phantom requeue (2 failed, task-guard passed).
- `.venv/Scripts/python -m ruff check src tests` → All checks passed!
- `.venv/Scripts/python -m mypy src` → 3 errors in `src/paa/sandbox/subprocess_backend.py:548` (pre-existing POSIX attrs on a Windows host; untouched file — matches AGENTS.md note; changed files clean).
- `.venv/Scripts/python -m pytest tests/conventions -q` → 33/34: `test_every_referenced_line_is_not_blank` fails on 10 citations to `recovery.py:285/:306/:366` (now blank after the +9 line shift at 256-272). Per brief, docs not edited — NEED filed below for ORCH sweep.

## NEED (docs line re-point, ORCH sweep)
All in the current tree: `src/paa/ledger/recovery.py:285` → `:294`,
`recovery.py:306` → `:315`, `recovery.py:366` → `:375` (contents: `#: ... A ``DENIED``
comment line, `) -> InterruptionState:` signature close, `def find_interrupted_calls(...)`).
Citing files: `docs/data/data-lifecycle.md:56`, `docs/flows/workflow-03-crash-recovery.md:46,53,58,81,150`,
`docs/modules/ledger.md:108`, `docs/runtime/background-jobs-and-events.md:56,65,104`.
Also semantically stale (lock-blind) after `store.py` +12 shift:
`ledger/store.py:385` → `:397` (`verify_chain`), `:435` → `:447` (`verify_all_chains`).
Agent.py citations were already shifted by the sibling N2.6 work before this task.

## Git state
Branch `ops/20261109-next-spec`. Code commit `357134a` (recovery.py, store.py,
agent.py incl. preserved sibling hunk, test_recovery_chaos.py). Report commit
staged separately below. Uncommitted sibling files (test_blobbing.py, work-ledger,
COMMS.md) left untouched.

