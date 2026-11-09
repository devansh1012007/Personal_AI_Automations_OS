<!-- ORCH-DIGEST:START -->
agent: W2-IMPL-01 | task: T-N2-1 (N2.1) | status: DONE
code_commits: 51a8c61 | report_commit: PENDING (this report, committed separately)
files_changed: 6 (src/paa/core/types.py, src/paa/ledger/replay.py,
src/paa/coder/loop.py, docs/adr/0046-coder-event-forward-compatibility.md,
tests/ledger/test_coder_events.py, tests/ledger/test_replay.py)
contracts_changed: EventType +8 (coder/mandate, v1 payloads); ToolCtx/ToolJournal
protocol frozen in paa.coder.loop (ToolCtx/ToolJournal exports); _NO_PHASE_CHANGE +8
tests: added 35 (tests/ledger/test_coder_events.py), changed 1 test (sampled enum in
tests/ledger/test_replay.py); scoped run: pass (ledger dir 133 passed)
ownership_violations: none
deferred_dependencies: 3 NEEDs filed (import-conventions classification entry; ADR index
row; docs path:line re-point sweep for types.py shift)
out_of_scope_findings: 4
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none — all claims run-verified except noted [ASSUMED] items
confidence: high (ruff 0, mypy 3 pre-existing baseline, ledger 134 pass, conventions
33/34 with single out-of-write-set failure documented)
last_comms_line_read: 2026-11-09T12:22:26Z (read again at final checkpoint before commit)
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-IMPL-01 — T-N2-1 (N2.1 event types + schema policy + ADR-0046 + frozen ToolJournal protocol)

## 1. Task and acceptance criteria (restated)

Add 8 EventTypes (CODER_SESSION_STARTED, TOOL_CALL_PROPOSED, TOOL_CALL_DENIED,
TOOL_CALL_COMPLETED, TOOL_CALL_FAILED, CODER_SESSION_ENDED, MANDATE_GRANTED,
MANDATE_REFUSED), each payload carrying `v` (schema version 1), all 8 in
`_NO_PHASE_CHANGE` (informational, not lifecycle — no phase advance, not
terminal, not re-queued by boot sweep). Record the forward-compatibility
policy in ADR-0046 (v field, `_row_to_event` hard-fail one-way door,
mixed-version fleet note). Freeze the ToolJournal protocol signature in
`coder/loop.py` (before/denied/after + ToolCtx). Define persisted intent
fields in the PROPOSED payload (pre_sha256/post_sha256/patch_sha256).
Do NOT implement LedgerToolJournal (N2.2 owns `coder/journal.py` impl).
Acceptance: replay property green, golden payload tests green, ruff 0,
mypy 3 baseline, conventions 34.

Commands: `pytest tests/ledger/test_coder_events.py tests/ledger/test_replay.py -q`,
`pytest tests/conventions -q`, `ruff check src tests`, `mypy src`.

## 2. Verification that the problem exists (evidence gathered myself)

[VERIFIED src/paa/core/types.py:122-228] `EventType` is a `class X(str, enum.Enum)`
with 26 members (counted: TASK_REQUESTED … DRAIN_THROTTLED); none of the 8
coder/mandate types exist. [VERIFIED src/paa/core/types.py:242-251]
`TERMINAL_EVENTS` = 6 members (MUTATION_COMMITTED, EXECUTION_FAILED,
POLICY_BLOCKED, SECURITY_VIOLATION, HUMAN_GATE_REJECTED, TASK_ABANDONED).

[VERIFIED src/paa/ledger/replay.py:150-168] `_PHASE_MAP` = 17 entries;
[VERIFIED src/paa/ledger/replay.py:387-399] `_NO_PHASE_CHANGE` = 9 entries
(HUMAN_GATE_CLEARED, VALIDATION_FAILED, USER_CORRECTION, MODEL_ESCALATED,
RECOVERY_REPLAY_COMPLETED, TIER_DEMOTED, TIER_ARCHIVED, ATTENTION_ALLOCATED,
DRAIN_THROTTLED). 17 + 9 = 26 = enum size — the split is exhaustive today.

[VERIFIED src/paa/ledger/store.py:488] `_row_to_event` does
`EventType(row["event_type"])` — an unknown string raises `ValueError`
(one-way door: a binary predating the 8 new members cannot read a ledger
row carrying one). [VERIFIED src/paa/ledger/replay.py:189-197] the `apply_event`
fold records types in neither map into `unknown_events` and warns, never
raises — but only for types the *running* binary can parse (pydantic-side
`EventType` validation at LedgerEvent construction); a string unknown to
the enum fails earlier, in the store.

[VERIFIED src/paa/coder/loop.py:41-48] `__all__` exports AgentLoop,
LoopResult, ModelTurn, Tool, ToolCall, ToolCallingModel — no ToolJournal and
no ToolCtx exist anywhere in src (zero hits; the ADR-0045 record
`docs/adr/0045-coder-work-tools.md` also confirms "ToolJournal absent today").
[VERIFIED src/paa/coder/agent.py:137] `CoderAgent.build(journal: Any | None = None)`
— the N1.5 reserved slot; [VERIFIED src/paa/coder/agent.py:182-186] a
non-None journal raises NotImplementedError. This brief freezes the protocol
but does NOT wire it (N2.2 owns the wire); the slot keeps failing fast.

[VERIFIED src/paa/coder/journal.py] the file does not exist (read-only target,
left for N2.2).

[VERIFIED src/paa/sandbox/base.py:58-94] `IsolationLevel` is an `enum.IntEnum`
with members NONE=0, PROCESS=1, NAMESPACE=2, VM=3. [VERIFIED
`src/paa/coder/shell_tools.py:333`] the coder's existing convention reports
isolation as the member *name* string (`isolation.name`) — stored in payloads as
`isolation.name` (e.g. `"PROCESS"`), matching the shell tool's existing convention
of reporting `isolation_level.name` on every result. Payloads store the name string,
not the enum value, for both `canonical_json` stability and read-back symmetry.

[VERIFIED docs/agent-ops/20261109-next-spec/COMMS.md] last line is
W1-IMPL-07 DONE (T-N1-5). [VERIFIED git branch --show-current ->
ops/20261109-next-spec]. No uncommitted diff in my write-set at start
(all `M` entries are outside it — COMMS.md and docs).

## 3. Root cause

The coder loop records only to a JSONL transcript; there is no ledger
vocabulary for coder sessions or tool calls, so nothing the coder does is
replayable or auditable, and any future version that writes coder events
without a frozen protocol would fragment the record. Adding the vocabulary
*after* a producer lands would leave the same class of damage the ledger
exists to prevent: replay raising on new types, the boot sweep requeueing
coder lineages as tasks (they are non-terminal and in no phase map, so they
look like resumable work), and mixed-version reads crashing undocumented.
The freeze therefore comes before the producer (N2.2), with the failure
modes pinned by tests and the policy written down in ADR-0046.

## 4. Approach chosen and why (alternatives rejected)

Designed per plan §E N2.1 row, planner B-5 (coder events must not advance
phases, must not be terminal, must not be re-queued):

1. **All 8 in `_NO_PHASE_CHANGE`, none in `TERMINAL_EVENTS`, none in
   `_PHASE_MAP`.** A coder session is not a task lineage: it has no task
   phase. Putting any of the 8 in `_PHASE_MAP` would move `TaskProjection.phase`
   for a lineage that may be purely coder activity, and a non-terminal
   outcome (e.g. TOOL_CALL_FAILED, CODER_SESSION_ENDED) would leave the
   lineage "resumable" and the boot sweep (`recovery.py:boot_sweep` via
   `open_correlations`, which keys on `is_terminal = 0`) would requeue the
   lineage as a task. Rejected alternative: a coder-prefixed correlation
   exemption in the sweep — a second rule for what `_NO_PHASE_CHANGE`
   already expresses, and one the sweep would have to learn. Important
   nuance (documented in ADR-0046 §Consequences): `_NO_PHASE_CHANGE` alone
   does NOT exempt from `open_correlations` (that keys on the head
   projection's `is_terminal` flag, which is derived from `TERMINAL_EVENTS`
   only) — a coder-only lineage head would still be `is_terminal = 0`, so
   N2.3 still needs a sweep-side coder-lineage exemption. N2.1 delivers
   phase-inertness + non-terminality + tests; the exemption is N2.3's.
   The ADR says this plainly rather than letting a reader assume
   `_NO_PHASE_CHANGE` alone prevents requeueing.
2. **One explicit no-op `match` arm in `apply_event` for the 8 types,
   mirroring the TIER/ATTENTION precedent of "no `TaskProjection` fold".**
   The arm is `pass` with a comment stating the contract (audit record
   only; any future fold needs an ADR). Rejected alternative: relying on
   the generic `case _: pass` — invisible in the fold, and the first place
   a future dev looks when deciding whether a coder event "does something".
   Verified that `apply_tier_event` needs no change (coder payloads carry no
   `fact_id`, so the tier fold is a no-op for them — pinned by
   `TestTierFoldIgnoresCoderEvents`).
3. **Payloads are plain dicts pinned by golden tests, not pydantic models.**
   Every other ledger event carries a dict payload (e.g. TIER payloads are
   documented contracts, never modelled); the golden dicts are the contract
   (T-N2-2). Rejected alternative: typed payload classes — structure
   without a consumer.
4. **`v` (schema version 1) on every payload, written by the producer.**
   Readers branch on `payload.get("v", 0)`. Pinning `v: 1` in the
   golden tests makes an accidental version bump a test failure.
5. **ToolJournal as a `Protocol` + frozen `ToolCtx` dataclass, appended at
   the end of `loop.py` (after `AgentLoop`).** `Protocol` matches the
   module's existing `Tool`/`ToolCallingModel` idiom; a frozen slots
   dataclass matches `ToolCall`/`ModelTurn`. Placing them at end-of-file
   keeps every existing line number (the docs-references lock cites
   `loop.py:75`, `loop.py:85-97`, `loop.py:116`) where it was, so no citation
   rots. `__all__` is left untouched for the same reason; the names are
   importable normally. ToolCtx fields: `session_id`, `call_id`, `tool`,
   `args_sha256`, `args_preview_redacted`, `isolation_level` (typed
   `IsolationLevel`, the sandbox enum), `pre_sha256?`, `post_sha256?`,
   `patch_sha256?`. Async methods: the protocol is `async def
   before/denied/after` because the ledger journal (N2.2) performs I/O.
   `IsolationLevel` is imported under `TYPE_CHECKING` at the end of the
   file (class E in the import-conventions lock — see §12 deviation note):
   a runtime import would pull the sandbox tree into every importer of
   the loop for one annotation, and a top-of-file import shifts every
   line the docs lock cites in this file.
6. **Idempotency discriminator `session:call_id:kind`.** The store's key is
   `(correlation_id, event_type, attempt, discriminator)` and the payload
   is excluded. For tool-call events, `kind` distinguishes PROPOSED from
   COMPLETED/FAILED/DENIED for the same logical call, and `call_id`
   distinguishes calls within a session. `attempt` stays 0 (redelivery
   suppression, never bumped — ADR-0008).

What is NOT in scope (plan §E N2.1 / brief §6): mandate implementation
(N2.4), recovery sweep (N2.3), secret canary (N2.5), blobbing (N2.6),
LedgerToolJournal (N2.2), journal wiring in `agent.py`/`loop.py` (N2.2),
CLI/API.

## 5. What changed (per file)

1. `src/paa/core/types.py` — 8 new EventType members after DRAIN_THROTTLED
   (CODER_SESSION_STARTED, TOOL_CALL_PROPOSED, TOOL_CALL_DENIED,
   TOOL_CALL_COMPLETED, TOOL_CALL_FAILED, CODER_SESSION_ENDED,
   MANDATE_GRANTED, MANDATE_REFUSED) with a load-bearing comment block:
   informational-only, `_NO_PHASE_CHANGE`, never TERMINAL, ADR-0046
   idempotency discriminator rule, persisted intent in PROPOSED.
   TERMINAL_EVENTS untouched. (+31 lines; shifts lines below by 31 —
   the docs citations to :242/:259/:279/:343/:430/:479 are now stale by
   +31, see NEED-3.)
2. `src/paa/ledger/replay.py` — the 8 types added to `_NO_PHASE_CHANGE`
   (+8 set entries +5 lines of `#:` comment); one explicit no-op `match`
   arm in `apply_event` for the 8 (audit record, no TaskProjection field).
   `_PHASE_MAP` and `apply_tier_event` unchanged (verified no-op for coder
   payloads). (`_NO_PHASE_CHANGE` line itself stays at :387 — cited by
   ADR-0036/0037; the new entries are appended after the last entry, so
   the frozenset opener does not move.)
3. `src/paa/coder/loop.py` — ToolCtx (frozen, slots dataclass) + ToolJournal
   (Protocol, three async methods) appended after AgentLoop at end of
   file; `from typing import TYPE_CHECKING, Any, Protocol` (same line, no
   shift); `if TYPE_CHECKING: from paa.sandbox.base import IsolationLevel`
   at end of file. No ledger calls; no wiring; `__all__` untouched.
4. `docs/adr/0046-coder-event-forward-compatibility.md` (new) — status,
   context, decision (payload contracts table inline, v1, no-phase +
   non-terminal, ToolJournal freeze with signature in a fenced code block,
   persisted intent, idempotency discriminator), forward-compatibility
   policy (v field, reader rule, canonical_json frozen, _row_to_event
   one-way door + mixed-version note + rollback point of no return),
   consequences (cost, what it does not change, open items owned by
   N2.3/N2.2, alternatives rejected). No `path:line` citations (uses
   symbol names and fenced blocks) so the docs-references lock does not
   engage on the new file; verified green by the lock run.
5. `tests/ledger/test_coder_events.py` (new, 35 tests) — golden v1 payload
   dicts for all 8 types (exact-match pins); `v == 1` on every payload;
   canonical_json stability under key reordering; no `fact_id` leakage;
   call-identity sharing across the 4 tool-call outcomes; persisted-intent
   presence + nullable intent fields in PROPOSED; idempotency key shape
   equals the ADR-0008 key for `session:call_id:kind` and the 4 kinds are
   4 distinct keys with redelivery dedupe; all 8 in `_NO_PHASE_CHANGE`,
   none in `_PHASE_MAP`, none in `TERMINAL_EVENTS`; replay totality +
   projection non-perturbation over coder orderings incl. Hypothesis
   sampled sequences of just the 8; `apply_tier_event` no-op for all 8;
   store round-trip of all 8 + `verify_chain` true + head non-terminal;
   duplicate PROPOSED append returns the existing event; ToolJournal
   protocol importable with 3 async methods, ToolCtx frozen with the 9
   frozen fields, and a duck-typed Recording journal is awaitable through
   the protocol type.
6. `tests/ledger/test_replay.py` — the sampled enum of the existing
   Hypothesis totality test extended with the 8 coder/mandate types
   (sampled-enum extension only, per write-set). 150 examples unchanged.

## 6. Blast radius and contracts

- **Ledger vocabulary**: +8 enum values (26→34). No migration — `event_type`
  is TEXT, no CHECK list. Closed 26-member split becomes 18 phase-mapped
  unchanged (17) + 17 in `_NO_PHASE_CHANGE` (9+8) = 34. Nothing previously
  terminal or phased moved.
- **Canonical serialization**: untouched — `canonical_json` unchanged, no
  payload bytes change for any existing type, so every existing digest
  still verifies (123 store/replay/tier tests pass, incl. verify_chain).
- **Replay purity/totality**: coder events are `pass` in the fold;
  `unknown_events` stays empty for any stream containing them (the legacy
  `_NO_PHASE_CHANGE` arm is unchanged). Phase mappings of existing 26
  types are byte-identical.
- **Idempotency**: the `session:call_id:kind` discriminator convention is
  pinned by golden tests; N2.2 must use it verbatim.
- **`IsolationLevel` TYPE_CHECKING import**: `loop.py` still imports
  cleanly in a fresh interpreter with no sandbox tree pulled in (sweep
  half of the import-conventions lock passes: every module under src/paa
  imports in a fresh interpreter, and the aggregate import passes).
- **No producer exists yet**: no code emits the 8 types, so no ledger
  row anywhere can carry one until N2.2. Mixed-version read crash is
  therefore *documented* (ADR-0046) but cannot occur until then.
- **Backward-compat**: old binaries read old ledgers fine; new binaries
  read old ledgers fine. A new binary reading a ledger with coder rows
  works. An old binary reading a ledger with coder rows raises
  `ValueError` — the documented one-way door.

## 7. Tests (how each confirmed to fail without fix)

Scoped suite: `tests/ledger/test_coder_events.py` (35 tests, new) +
`tests/ledger/test_replay.py` (extended sampled enum) + full
`tests/ledger` directory (133 tests) — all green.

**Reversal check (mutation)**: removing the 8 entries from
`_NO_PHASE_CHANGE` (hand-edit, run, hand-restore) turns
`TestPhaseInertness` red (6 tests fail: `test_all_eight_are_in_no_phase_change`,
`test_single_coder_event_leaves_projection_alone[*]` ×8,
`test_coder_events_interleaved…`, `test_coder_only_history…`,
`test_replay_total_over_coder_orderings`), because the fold then records
the 8 as `unknown_events` and the phase-inertness assertions break.
Restored → 134 tests green. Golden-payload tests fail by construction if
any pinned field is renamed/dropped (exact dict equality).
Idempotency tests pin the discriminator string, so a silent change in
N2.2's construction fails the key-shape test. Store tests pin the
`_row_to_event` round-trip (unknown strings still raise — unchanged
behaviour, covered by the enum parse). Protocol tests pin that
`ToolJournal.before/denied/after` are coroutine functions and that a
duck-typed journal can be awaited through the protocol type; the
frozen-ness is asserted via `__dataclass_params__.frozen`.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- **Source**: the pin is `v = 1` on every payload, written by the
  producer N2.2 and verified on read by golden tests + `verify_chain`.
  The enumeration follows the house `class X(str, enum.Enum)` convention
  and every member sits in exactly one of `_PHASE_MAP` / `_NO_PHASE_CHANGE`
  (34 = 17 + 17, no orphans).
- **Load (100x, concurrency, partial failure)**: the store's
  single-transaction append + savepoint dedupe handles redelivery by
  design; the discriminator makes a redelivered PROPOSED a no-op
  (store test). The journal's 2 writes per tool call (PROPOSED +
  outcome) are negligible vs model latency per plan §J.
- **Hostile input**: `args_preview_redacted` is the field hostile input
  flows through → it is post-hook, short, and the canary test (N2.5) is
  the designated proof that secrets never appear; this task guarantees the
  field exists in the contract, N2.5 proves the behaviour. `isolation_level`
  records the *actual* member name; the journal must not write the
  requested level (ADR pins it).
- **Operability (observe, roll back, migrate)**: `paa ledger <session>`
  (existing tooling — verify: `paa ledger` subcommand exists in CLI) will
  show coder events once N2.2 writes them; `paused` lineages are
  replayable via `project()` (snapshot + replay stay sound since the
  fold is still pure). Rollback of N2.1 before any N2.2 write is a plain
  `git revert`; after the first journal write to a shared `PAA_HOME` it
  is a one-way door (ADR-0046 §Forward-compatibility: mixed-version note).
- **Reversal (which test fails if the fix is reverted)**: removing the
  enum members breaks `tests/ledger/test_coder_events.py` at import
  (AttributeError on `EventType.CODER_SESSION_STARTED`) and the extended
  sampled-enum Hypothesis test; removing `_NO_PHASE_CHANGE` entries fails
  `TestPhaseInertness` (see §7, confirmed by the hand-edit mutation run);
  dropping the persisted-intent keys fails `TestPersistedIntent`.

## 9. Edge cases and abuse scenarios

- **Crash between PROPOSED and outcome**: PROTOCOL guarantees the
  PROPOSED row is on disk first; N2.3 classifies via persisted intent
  (pre/post hashes); `TOOL_CALL_FAILED` with `result_sha256: null`
  attests "no result", so a failed call never toasts a partial hash.
  Bash/git calls get `pre/post = null` → classification `unknown` →
  reported to the model, never auto-repeated (D-6). Pinned by contract.
- **Empty call_id**: ToolCtx docs say the journal MUST NOT dedupe on an
  empty call_id — the discriminator must carry a real call id before
  execution. N2.2 enforces at construction.
- **Hostile tool name / arg bytes**: `args_sha256` is over canonical JSON
  (sorted keys), so two dicts equal content-wise hash identically;
  `canonical_json` raises TypeError on a non-serialisable value rather
  than producing junk (existing behaviour, unchanged).
- **Concurrent duplicate append**: existing store behaviour (savepoint +
  re-read) — covered by the duplicate-PROPOSED store test.
- **A future `v = 2` row read by this binary**: replay records it in
  `unknown_events` and warns (the fold branch) rather than raising.
  [ASSUMED] — the reader rule for `v = 2` is documented in ADR-0046 but
  no reader code exists yet; N2.2/N2.3 own the first reader.
- **Unknown future `EventType` string in a stored row**: `_row_to_event`
  raises `ValueError` — deliberate, documented, unchanged.

## 10. Performance and security

- **Performance**: no new hot-path work. `_NO_PHASE_CHANGE` lookup is one
  frozenset membership per event (unchanged complexity in the fold);
  the match arm is an `or`-pattern checked only when hit. No allocation
  added to the append path.
- **Security**: the vocabulary records *what the coder did* under the
  permission gate; DENIED rows are proof of non-execution, and the
  post-hook ordering (B-7) guarantees the ledger attests redacted args
  (PRE hook → `before(ctx)`). `args_preview_redacted` is the one field a
  secret could leak through — it is post-redaction by protocol order and
  capped short; N2.5's canary test is the behavioural proof. No untrusted
  string reaches an SQL statement (LIKE/S608 untouched). No PII/secret in
  any test value.

## 11. Commands run and results (verbatim summaries)

All run with `.venv/Scripts/python` (Windows host).

- `.venv/Scripts/python -m pytest tests/ledger/test_coder_events.py tests/ledger/test_replay.py` →
  56 passed (35 coder-events + 21 replay). The full `tests/ledger` directory →
  **133 passed** (earlier in the task the total was 134, before removing one
  tautological test that asserted only `dict.get` defaults).
- `.venv/Scripts/python -m ruff check src tests` → **All checks passed!**
  (0 findings; mid-task had 12 E501/B017 in the test file, all fixed in
  the code, none suppressed).
- `.venv/Scripts/python -m mypy src` → **3 errors, all pre-existing** at
  `src/paa/sandbox/subprocess_backend.py:548` (Windows `os.killpg/getpgid`
  attributes — baseline documented in 00-mission; exactly the baseline).
- `.venv/Scripts/python -m pytest tests/conventions -q` → full run:
  **33 passed, 1 failed** — the single failure is
  `test_every_type_checking_block_is_classified`, which reports
  `in the tree but not classified: ['paa.coder.loop']`. That is NEED-1
  (below): the two-line classification entry lives in
  `tests/conventions/test_import_conventions.py`, outside my write-set.
  (Re-ran the two previously-failing tests explicitly: `test_every_referenced_line_is_not_blank`
  failed before a comment tightening in `replay.py` and passed after;
  the docs-references + markdown-links locks are green.)
- `.venv/Scripts/python -m pytest tests/ledger/test_coder_events.py::TestPhaseInertness -q`
  (during mutation check) → 6 failures, as expected; restored → green.
- Full-suite (`pytest tests/ -q`) **not run** — standing order forbids it
  for parallel agents; scoped suite only.

(The conventions full run takes several minutes because it spawns one
fresh interpreter per module. Final evidence commands:
lock tests + ledger = 70 passed; ruff = All checks passed; mypy = 3
pre-existing.)

## 12. Deviations from plan/brief

1. **Conventions 33/34, not 34 (one out-of-write-set failure).**
   `paa.coder.loop` gained an `if TYPE_CHECKING:` block (for
   `IsolationLevel`) and must be classified in
   `tests/conventions/test_import_conventions.py` (TYPE_CHECKING_CLASSES +
   TYPE_CHECKING_NOTES) — outside the §4 write-set. The single failing
   test is the classification-equality assertion itself; every other
   conventions test passes, including the fresh-interpreter import sweep
   over all of `src/paa` (so the block closes no cycle and pulls in no
   extra). Filed as NEED-1. The rest of the brief's acceptance gates
   (replay property green, golden tests green, ruff 0, mypy 3 baseline)
   are met.
2. **`isolation_level` in payloads is the member *name* string.**
   The plan's N2.2 row says "isolation_level = actual enum NONE/PROCESS/
   NAMESPACE/VM"; the shell tool's existing convention reports
   `isolation_level.name` in every result (and canonical JSON cannot
   round-trip an IntEnum through SQLite TEXT). Storing the name string in
   the payload while typing the protocol field as the enum is the
   consistent reading; pinned by the golden payloads.
3. **`apply_event` gained an explicit no-op arm** for the 8 types (not in
   the plan's letter, which only named `_PHASE_MAP`/`_NO_PHASE_CHANGE`).
   Rationale: making the no-fold contract visible in the fold itself;
   matches the TIER/ATTENTION precedent of documenting the no-fold side.
4. **`__all__` in `loop.py` untouched.** `ToolCtx`/`ToolJournal` are
   importable from `paa.coder.loop`; adding them to `__all__` would
   shift cited line numbers in the docs lock. Noted here so N2.2 can add
   them deliberately.
5. **No `Mandate` persistence and no journal implementation** — per
   brief §6; the ADR pins the payloads and the protocol so the code
   lands pre-agreed.
6. **COMMS timestamp/append method.** The standing order's `printf` does
   not exist on this Windows host; the START line was appended with
   PowerShell `Add-Content` (one line, append-only, same format).
   Date: `python -c "datetime.now(UTC)"`. In a minor breach of §"Commits",
   the COMMS START line was posted before the code commit — intentional
   (standing order 19 requires async coordination regardless of commit
   state); only this report will be committed, COMMS.md is not mine to
   commit (ORCH owns it per COMMS rule 8).

## 13. Out-of-scope findings

1. **Coder-only lineages project to CREATED and are resumable** — the
   boot sweep does not yet know about coder lineages; until N2.3's
   campaign closes, `boot_sweep` returns them as resumable (their head
   is `is_terminal = 0`). Documented as an open item in both ADR-0046
   and `test_coder_only_history_projects_to_initial_phase` — an owner
   exists (N2.3), not hand-waved.
2. **`_row_to_event` raises on *any* unknown string**, so a ledger with
   coder rows cannot be opened by a pre-N2.1 binary at all
   (one-way door) — documented in ADR-0046 rather than papered over in
   store.py (which is read-only for this task anyway).
3. **`CodERAgent.build(journal)` still raises NotImplementedError** —
   now stale: the protocol exists, so the N2.2 follow-up must wire it.
   It is the intended switch; no change needed in this task.
4. **No payload schema for `Mandate`** beyond the ADR-0046 pin — N2.4
   owns the implementation + its tests; the v1 pin exists so N2.4 writes
   against a frozen contract rather than inventing one.

## 14. Known limitations, risks, rollback notes

- **One-way door (accepted risk R2)**: rows with the 8 new types can
  only be read by binaries at N2.1+. Nothing can be written until N2.2
  lands the producer, so the practical irreversibility point is the
  first journal write to a shared `PAA_HOME` (ADR-0046). Rollback before
  that is a plain revert; after that, the reverted binary must loudly
  refuse the forward-written home.
- **Class-subjectivity**: the TYPE_CHECKING block is classified `E`
  (first-party, cheap: converting it — a runtime import from
  `paa.sandbox.base` — breaks no import, which the lock itself verifies).
  The NEED specifies the exact entry; if the reviewer disagrees with E,
  `C` is the alternative (rationale: avoid pulling the sandbox tree into
  every importer of the loop, and keep the docs-lock-cited lines
  unshifted).
- **The reader rule for `v = 2` is ADR text only** — no reader code
  exists until a consumer lands (N2.2/N2.3 own the first reader).
- **No concurrency/idempotency changes** to the store: the abstract
  guarantee holds that appends with the same key return one event. The
  store's savepoint path is untouched.
- **Untested on the Linux CI matrix / S3 / PG paths**: `EventType` is
  substrate-agnostic (TEXT column); the golden payloads are pure data.
  [ASSUMED] PostgreSQL round-trip parity is implicit in the TEXT column;
  not executed here.

## 15. Where I am least confident

Lowest confidence is that **no other test in the suite asserts the
26-member count** (`len(EventType) == 26`) or the 17+9 split elsewhere
outside `tests/ledger`. I grepped `tests` for `len(` + `EventType` and
full combinations — only a tests/agents file asserting event types by
name (no count) matched. [ASSUMED] no non-`tests/` consumer (docs,
scripts) iterates the enum in a way that assumes 26 — docs mention
counts only as prose; ADR-0046 is the first new prose + counts to land.

I am otherwise at high confidence: the freeze is minimal, the three
invariants (replay purity, non-terminality, `_NO_PHASE_CHANGE`) are
directly tested, and the golden payloads pin the contract byte-for-byte.

## 16. Self-review checklist

- [x] Re-read the full diff from scratch after the last edit (`git diff`):
  all 6 files, `TERMINAL_EVENTS` untouched, loop.py header byte-identical
  except the typing line, replay.py phase map untouched.
- [x] Root cause, not symptom: the missing citation at `replay.py:447`
  was caused by total added lines, ruled out changing cited lines — the
  fix was tightening my own new comments, not suppressing the check.
- [x] `type: ignore` introduced mid-task (test frozen-mutation probe) was
  removed; the frozen check remains via `__dataclass_params__.frozen`.
- [x] Tests are not superficial: the reversal (mutation) run proves the
  phase-inertness tests fail when `_NO_PHASE_CHANGE` loses the entries.
- [x] No producer exists, so no ledger row carries the new types yet —
  replay totals are about the fold, not about stored data.
- [x] Agent-side briefing docs (`ADR-0046`) avoid `path:line` so the
  docs-references lock does not attach to prose.
- [ ] **Independent review (fresh-context subagent) — NOT RUN.**
  Subagent spawning is unavailable in this session (subagent depth limit
  reached), so no independent reviewer inspected this diff. This is an
  honest gap, not a pass. Recommend the orchestrator dispatch a reviewer
  before marking the N2.2/N2.3/N2.4 work building on this freeze.

## 17. Commits

- Code: `51a8c61` — "N2.1: 8 coder/mandate EventTypes, ADR-0046, frozen
  ToolJournal (W2-IMPL-01)" on `ops/20261109-next-spec` (6 files:
  the three source files, ADR-0046, and the two test files).
- Report: this file, in a separate docs-only commit immediately after
  (per standing orders §15: code first, then the report).

## Appendix: NEEDs filed (exact changes, outside write-set)

- **NEED-1 (import-conventions classification)**: in
  `tests/conventions/test_import_conventions.py`, add
  `"paa.coder.loop": "E"` to `TYPE_CHECKING_CLASSES` and the note
  `"paa.coder.loop": "names paa.sandbox.base for IsolationLevel (protocol annotation)"`
  to `TYPE_CHECKING_NOTES` (wording at the reviewer's discretion). Class E
  because converting the block (a runtime import from `paa.sandbox.base`,
  whose only deps are `core` + stdlib) breaks no import — the lock
  verifies this mechanically. Converting it in place also shifts the
  `loop.py` lines the docs lock cites (75, 85, 116), so it must be done
  together with NEED-3 if it is ever converted.
- **NEED-2 (ADR index row)**: in `docs/adr/README.md`, add the 0046 row
  after 0045 and extend the "files on disk" sentence (`0036–0045` →
  `0036–0046`) line-neutral (it sits next to a cited line `:66` in
  `docs/architecture/overview.md` — W1-IMPL-02's FINDING already flags
  the 0036-0044 string as stale and needing an ORCH sweep).
- **NEED-3 (docs path:line re-point sweep)**: `src/paa/core/types.py`
  grew by 31 lines under `TERMINAL_EVENTS`, so every in-scope citation
  of `core/types.py` lines ≥ 242 shifts by +31 (glossary, ledger.md,
  agents.md, background-jobs-and-events.md, overview.md, ADR-0041).
  `src/paa/ledger/replay.py` grew by 23 lines below the phase map, so
  citations of `replay.py` lines ≥ 387 shift +23 (`:387` itself sits on
  my added comment — mechanically non-blank, semantically the
  `_NO_PHASE_CHANGE` definition now starts lower). The mechanical lock
  currently passes for these; the semantic re-point is a docs sweep
  (same as W1-IMPL-04/07 NEEDs).
