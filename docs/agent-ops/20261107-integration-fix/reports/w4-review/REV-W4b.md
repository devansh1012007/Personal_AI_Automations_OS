<!-- ORCH-DIGEST:START -->
agent: REV-W4b | scope: wave-4 fixes dc06ed5..HEAD (8971e83 + docs 48b9b5a/9e8e688) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 1 | MINOR 4 | NIT 1
claims_audit: ~30 claims checked / 3 unsupported-or-false (1 combined-green claim contradicted; 1 missing disclosure of order-pollution; 1 strftime baseline understated only by omission in §5 combined line)
band_aids_found: none in src; test-side: incomplete settings stub in new callsite tests pollutes paa.context.gatherer (MAJOR below)
top_3_risks: (1) new allocation-callsite tests break pre-existing creator test when run before it in same process (order-dependent, full-suite-masked by collection); (2) v5-shape corrupt blob still wedges v6 SQLite migration pre-backfill (NEED correctly deferred, tracked); (3) ~65 lock-blind stale orchestrator cites + agents.md:59 invisible :688/:737 remain wrong-but-green (sweep FINDING disclosed)
optimizations: DO-NOW 1 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only reviewer; reversal semantics checked against test bodies, not executed by revert)
confidence: high (full diff + surrounding source read blind; all fix test files + casualties + locks re-run independently with real results quoted; PG NOT-RUN — no DSN)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W4b (fresh independent, read-only re-review of wave-4 fixes)

## 0. Method
- Diff reviewed: `dc06ed5..HEAD` = 8971e83 (code fix) + 48b9b5a and 9e8e688 (docs) + 3 report commits.
- Read first, in order: STANDING-ORDERS.md, templates/review-code.md, COMMS.md (through line 131, last line read:
  W4-FIX-DOCS2 DONE), then the full src+tests diff, then prior findings REV-W4 §§4–5 and TREV-W4 §§5–6,
  then the three fix reports W4-FIX-01 / W4-FIX-DOCS / W4-FIX-DOCS2 (read AFTER the blind pass).
- Blind pass, per brief: graph norm columns (INSERT+UPDATE, same fold_case), cross-writer tests, TUI 3 keys +
  shared formatter (string duplication), claim guard, backfill corrupt count-log, each C1–C5 diff, docs re-point
  spot-reads (≥8). Then claims audit vs the three fix reports, with FIX-01's deferred NEED and residuals
  R-W4-002/006 handled explicitly.
- Commands run (all scoped; no full suite, no containers, no shared DB, no commits, source untouched):
  - [RAN `.venv\Scripts\python.exe -m ruff check src tests` -> All checks passed! (0)]
  - [RAN `.venv\Scripts\python.exe -m mypy src` -> exactly 3 errors, all `src/paa/sandbox/subprocess_backend.py:548` (baseline; gate holds)]
  - [RAN `pytest tests/agents/test_allocation_callsite_p007.py tests/storage/test_graph.py tests/tui/test_dashboard.py tests/storage/test_dialect.py tests/memory/test_creator.py tests/memory/test_facts.py tests/storage/test_tier_migration_p009.py -q` -> 2 FAILED (see §1.6/§4 R-W4b-001/002); all other dots pass]
  - [RAN `pytest tests/memory/test_entity_resolution.py -q` -> pass]
  - [RAN `pytest tests/conventions/test_docs_references.py -q` -> pass (docs lock green at HEAD)]
  - [RAN `pytest tests/conventions/test_import_conventions.py -q` -> pass (21)]
  - [RAN `pytest tests/agents/test_allocation_callsite_p007.py -q` -> pass isolated; `tests/storage/test_graph.py -q` -> pass isolated; `tests/storage/test_tier_migration_p009.py -q` -> pass isolated]
  - [RAN pair orders: `allocation + TestVectorSinkSemanticRoundTrip` -> 1 FAILED; reverse order -> pass; with `tests/context/test_gatherer.py` collected first -> pass (full-suite masking proof)]
  - [RAN `pytest tests/tui/test_dashboard.py::test_ledger_with_events -q` -> FAILED (pre-existing strftime typo, present at dc06ed5; baseline category-B)]
  - [RAN 10 docs spot-reads: facts.py:214, orchestrator.py:397/352/468/718/780, facts.py:363, creator.py:1515, daemon.py:146, replay.py:422 -> 10/10 semantic match with citing sentences]
  - [RAN companion spot-reads: orchestrator.py:512/574/667/741/797 (.run sites), :400, :581, :910 -> match]
- Branch verified: `ops/20261107-integration-fix` [RAN `git branch --show-current`].
- No source, test, or docs edits by me; only this report file written.

## 1. Blind-pass findings (before reading implementer reports)

### 1.1 Graph second writer (R-W4-004) — fixed correctly
- [VERIFIED src/paa/storage/graph/sqlite_backend.py:55 imports `from paa.storage.relational.dialect import fold_case` —
  the same seam `facts.py:88` uses; `fold_case` defined at dialect.py:113.]
- INSERT now carries `canonical_name_norm, aliases_norm` computed in the same statement: `canonical_norm =
  fold_case(canonical_name)`, `aliases_norm = "[]"` (the only fold the signature can compute; matches
  `facts._aliases_norm_value([])` = `"[]"`). DO UPDATE refreshes `canonical_name_norm` from `excluded` and
  deliberately does NOT touch `aliases_norm` (and never did touch `aliases`): refreshing with `"[]"` on
  conflict would erase facts-written aliases — a correct, documented deviation from REV-W4's literal
  "(both INSERT and DO UPDATE)" wording, which assumed the backend owned the alias list (it does not).
- Same-function check: facts uses `canonical_norm = fold_case(name)` (facts.py:435) +
  `_aliases_norm_value` (facts.py:436/258, also `fold_case` per alias). Graph uses the identical `fold_case`.
  One normalisation point preserved. No duplicated fold logic.

### 1.2 Cross-writer tests — three, two directions + rename
- `test_graph_upserted_entity_resolves_through_facts`: graph writes "Éclair Project" (non-ASCII pins the real
  fold, not engine `lower()`), facts resolve finds it exact; stored norms asserted (`éclair project`, `[]`).
- `test_facts_upserted_entity_is_visible_to_graph_reads`: facts-written entity appears as graph neighbour;
  facts alias still resolves (norm+raw written together on the facts path).
- `test_graph_rename_refreshes_the_fold_for_resolution`: rename updates the fold; old name no longer resolves.
- [RAN test_graph.py isolated -> pass.] Kuzu correctly excluded (its own node table; [VERIFIED grep hit only
  sqlite_backend.py in src/paa/storage/graph/]).

### 1.3 TUI mirror (R-W4-003) — fixed correctly
- Runner populates exactly the 3 keys from read-only `reflection_health(settings.home, enabled=…)`; `getattr`
  chains degrade a reflection-less settings fake to disabled instead of breaking the 2s tick.
- Dashboard's `_reflection_doctor_value` now builds a health dict and calls the shared
  `format_reflection_health` (reflection.py:240, signature `health: Mapping[str, Any]` — a plain `dict` literal
  satisfies `Mapping`; no MappingProxy needed or used). Duplicated strings are gone:
  [VERIFIED grep dashboard.py for stale/disabled/unknown -> only docstring/comment/key-mapping lines, no
  re-spelled values]. Renderer and population are pinned by 5 new tests incl. a real-marker runner test.

### 1.4 Claim guard (R-W4-007) — narrow, correct scope with a test-side cost (§4 R-W4b-001)
- `allocate_attention` wraps only `queue.claim` in try/except Exception → `log.warning("allocator.claim_failed")`
  + `AllocationResult(outcome="underfed", claimed=0)`. Matches REV-W4's recommended shape and the house
  degrade posture; `Exception` (not `BaseException`) is the right width. Docstring updated ("never raises for
  poison/empty or a claim failure").
- `run_allocation -> AllocationResult` via the EXISTING classified `TYPE_CHECKING` block
  (orchestrator.py:46; module has `from __future__ import annotations` at :21, so the annotation is lazy and
  safe). No new block, so the import lock needed no reclassification; [RAN test_import_conventions -> pass].
- The 3 new callsite tests are behaviour-real (real orchestrator + real ledger + scripted queue) and the
  raising-queue test pins the guard. BUT the stub mechanism is order-unsafe — see R-W4b-001.

### 1.5 Backfill corrupt count-log (R-W4-008) — honest middle, honestly scoped
- `_backfill_entity_norm` counts rows whose `aliases` is non-str/unparseable/non-list as corrupt, keeps the
  `'[]'` fail-soft write, and logs `migration.backfill_alias_corrupt` (corrupt_rows, total_rows) only when >0.
  Module-level structlog logger added (the backfill is a module function without access to the runner's log).
- Test construction is honest: it forces the crash-mid-step state (columns present, norms NULL) because that
  is the only shape where the backfill can see a corrupt row on SQLite. This immediately exposes the
  deferred NEVER-REACHABLE case (v5-shape corrupt blob wedges `ALTER TABLE ADD COLUMN` on a STRICT table
  before the backfill runs) — assessed in §2/§5 as a correct out-of-scope call.

### 1.6 Casualtie updates — none weakened (each C1–C5 diff read)
- C1 creator drain_105: 100 then 0 + `memory.embedding_drain_throttled` log, 5 still pending, fresh creator
  drains 5. Preserves drain-not-OFFSET intent; adds a stronger assertion (the log line). Not weakened.
- C2 dialect: literals moved to `e.canonical_name_norm`/`e.aliases_norm`; helper table gains norm columns +
  norm-indexing trigger + fold written for its row; hostile-term execution still runs against the new shape.
  [RAN test_dialect.py in combined run -> pass.]
- C3a corrupt row: re-pinned against `aliases_norm` (the now-corruptible column); PRAGMA pragma juggling
  removed because the norm column has no CHECK. Fail-closed intent preserved at the correct column. Not weakened.
- C3b gate model: raw INSERT writes norm columns; assertion now compares `_trigrams(fold_case(stored))`
  against FTS tokens — the gate-vs-index invariant faithfully adapted to the norm token source (FTS indexes
  the fold), not relaxed. Not weakened.
- C4 version pin: 5->6 with history rationale in the docstring; name kept historical. Not weakened.
- C5 entity_resolution: `_insert` writes fold columns (fixture fix, same totals/orders), and the
  escaped-blob test FLIPS to assert resolution through the norm plus norm == `'["ééé"]'` with the raw blob
  still escaped and the gate score unchanged — asserts more than before, with justification as the
  repaired-divergence pattern. Not weakened. [RAN test_entity_resolution.py -> pass.]

### 1.7 Docs re-points — spot-reads pass semantically
- 10 spot-reads [RAN above]: facts.py:214 `def _trigrams`, orchestrator.py:397 `async def run`,
  :352 `def _classify`, :468 `async def _hydrate`, :718 `async def _execute_steps`, :780 re-project,
  facts.py:363 `async def upsert_entity`, creator.py:1515 `drain_pending_embeddings`, daemon.py:146
  `queue.claim`, replay.py:422 `async def project` — all match their citing sentences. 8 companion `.run`
  call sites + :400/:581/:910 also match. Numbers-only diffs, no prose rewrites.
- [RAN docs lock -> pass at HEAD.] The lock proves non-blank + in-range; my spot-reads re-verify the
  semantic half for a sample — DOCS2's "never blind-shift" rule is visible in the mapping (e.g. :515 -> :718
  by symbol, not +1).

### 1.8 Residuals — correctly NOT re-landed
- [VERIFIED grep `run_allocation` src/ -> 1 hit = definition only; `DRAIN_THROTTLED` src/ -> types.py
  definition + comment + replay vocab only.] R-W4-002 (scheduling) and R-W4-006 (DRAIN_THROTTLED emitter)
  remain deferred and tracked; no scheduler, producer, or emitter was added. PASS.

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| Graph fix uses same fold_case as facts; INSERT+UPDATE consistent | Yes | §1.1; DO UPDATE aliases_norm preservation is a correct deliberate deviation, documented at the SQL |
| 3 cross-writer tests cover both directions + rename | Yes | §1.2; [RAN test_graph.py -> pass] |
| TUI: 3 keys + shared formatter, duplicated strings gone | Yes | §1.3; [VERIFIED no re-spelled values in dashboard.py] |
| Claim guard degrades to underfed, never raises; run_allocation typed | Yes | §1.4; TYPE_CHECKING reuse keeps lock green [RAN] |
| Backfill count-log fires with count; migration completes | Yes for reachable states; No for v5-shape SQLite | §1.5/§5; test honestly pins crash-mid-step shape only |
| NEED deferred: v5-shape STRICT wedge cannot reach backfill | Yes — correct scope call | §5: pre-repair step = new guarded step + PG probe question, outside the count-log-only write-set; NEED posted, not silently fixed, not silently landed |
| C1–C5 deliberate updates, none weakened | Yes | §1.6, each diff read; [RAN all casualty files -> pass except pre-existing strftime] |
| "Proxy report verified at HEAD; 10-file combined run all pass" (FIX-01 §5) | NO — contradicted | [RAN 7-file combined incl. allocation+creator -> 2 FAILED; pair re-run reproduces pollution failure]. Claim as written is false on this host; pollution undisclosed. See R-W4b-001. |
| "test_ledger_with_events is pre-existing category-B, out of write-set" | Yes | [VERIFIED dc06ed5 dashboard.py has same `%H:%:%S`; W3 disclosed as known strftime failure]. Omission of it from the §5 combined-run line is sloppy but not a falsehood about the fix. See R-W4b-002. |
| Docs: 51+62 re-pointed, lock 5 passed, numbers-only | Yes | [RAN docs lock -> pass]; §1.7 spot-reads; DOCS2 verified "zero references to the other 5 shifted files" claim shape by enumeration in their report — consistent with diff census (only orchestrator had citations) |
| Docs: 65 lock-blind stale + agents.md:59 :688/:737 invisible | Yes, accurately disclosed | Not spot-verified exhaustively, but the two named cites were checked (line-59 yield-zero claim is plausible; the :737 blank target is within the file I read). Residual tracked as FINDING, not hidden. See R-W4b-003. |
| "ruff 0; mypy 3 = baseline" | Yes | [RAN both -> 0 / 3 baseline] |
| Residuals R-W4-002/006 untouched, tracked for W5 | Yes | §1.8 |
| Import conventions 21 passed | Yes | [RAN -> pass] |

Claims checked: ~30 · unsupported-or-false: 3 (the combined-green line; the undisclosed pollution hole behind
it; the lock-blind count discrepancy between DOCS 25-vs-22 is examined in their report and is immaterial).

FIX-01's NEED (v5-shape corrupt blob wedges v6 `ADD COLUMN` under STRICT): the scope/out-of-scope call is
CORRECT. The report's scratch-verified mechanism (STRICT + CHECK re-validation on ADD COLUMN) is the right
root cause, the brief scoped `migrations.py` to count-log only, and the pre-repair step needs a new guarded
migration step plus a PG-specific validity probe — a behaviour change beyond the write-set. Carrying it as
NEED rather than fixing it silently, and labelling the count-log's reach honestly in the test docstring, is
exactly the conduct the standing orders require. The residual risk is real (any v5 DB with a foreign-writer
corrupt blob is stuck at startup with a retry loop) but it requires a CHECK-bypass foreign write, so
deferring with a named owner is proportionate. The NEED needs a named W5 task (see R-W4b-004).

## 3. Long-run test (independent answers)
- **Source:** all folds flow from the one `dialect.fold_case`; the graph writer now honours it; docs and
  memory writes agree. TUI/CLI render the same value from the same home. Allocation decisions stay ordinary
  ledger events; replay totality untouched. Backfill is deterministic and re-runnable.
- **Load (100x / concurrency / partial failure):** graph upsert remains one statement; claim guard adds one
  bounded attempt. The polluted-stub hazard (R-W4b-001) is the only load-sensitive element: it is masked by
  collection-time import order in the full suite and breaks narrower runs — fix by hoisting `allocate_attention`
  to test-module scope (package `__init__` then binds the original before any monkeypatch) or by returning a
  full settings object. Backfill remains a full-table `fetchall` in one transaction (O3 DO-LATER stands).
- **Hostile input:** corrupt norm rows fail closed (C3a); hostile terms still never reach SQL (dialect suite
  green); malformed marker degrades to unknown. One new hostile-adjacent shape: `queue.claim` raising a
  non-Exception (e.g. `KeyboardInterrupt`/`SystemExit`) still propagates — correct; programming errors
  (TypeError from a bad backend) now read as `underfed` + warning, which matches the house posture but means
  a mis-wired backend looks like "nothing to decide" until logs are read (MINOR memo; the warning line makes
  it observable).
- **Operability:** roll back = revert 8971e83 (old code ignores the nullable norm columns); TUI value now
  truthful; corrupt count visible in migration logs; STRICT wedge rollback is "fix the blob with a pragma
  write, then migrate" — needs a runbook line because the retry loop otherwise prints the same error forever.
- **Reversal (which test fails if the fix is reverted?):** revert `_UPSERT_ENTITY_SQL` -> 3 cross-writer
  tests fail (norm NULL -> invisible). Revert runner keys -> 2 runner tests + 1 render tripwire fail with
  `disabled`. Revert claim wrap -> raising-queue test raises `RuntimeError` uncaught. Revert count-log ->
  corrupt-blob test finds no log line. Revert any C1–C5 edit -> its stale pin fails again in the opposite
  direction (each edit is load-bearing, not decorative). R-W4b-001 reversal: deleting the new test file makes
  the pair-order failure disappear — proving the pollution is the file's fault, not the source's.

## 4. Findings
### R-W4b-001 [MAJOR] New callsite tests pollute `paa.context.gatherer` — order-dependent green
- Location: `tests/agents/test_allocation_callsite_p007.py` (`_enable` stub), mechanism via
  `src/paa/context/__init__.py:42` + `src/paa/context/gatherer.py:30`. · Evidence: [RAN pair
  `allocation + TestVectorSinkSemanticRoundTrip` -> 1 FAILED: `AttributeError: SimpleNamespace has no attribute
  'context'` at gatherer.py:327; reverse order passes; inserting `tests/context/test_gatherer.py` first makes
  it pass (collection-time import binds the real `get_settings` before any monkeypatch)]. · Why it matters:
  `run_allocation` lazy-imports `paa.context.allocator`, which runs `paa/context/__init__`, which imports
  ` BoundedContextGatherer`, which binds whatever `paa.config.get_settings` is CURRENTLY — during the patched
  window that is the lambda returning `SimpleNamespace(allocator=…)`. The lambda outlives the test in
  gatherer's namespace after monkeypatch undoes `paa.config`. A later test in the same process constructing
  `BoundedContextGatherer()` then crashes. This breaks any narrower invocation order (e.g. `pytest
  tests/agents tests/memory`) and violates the AGENTS order-independence convention — and FIX-01's claim that
  the 10-file combined run "all pass" is therefore false on this host, as reproduced. The full suite is
  green only because collection of gatherer tests binds the original first. · Recommended fix (either):
  hoist `from paa.context.allocator import allocate_attention` (or import `paa.context`) to the new test file's
  module scope so package init binds the real `get_settings` at collection time; or stub with a full settings
  object carrying `.context` (e.g. `get_settings()` real object with `allocator` replaced). Then re-run the
  pair in the failing order to prove order-independence.

### R-W4b-002 [MINOR] Pre-existing strftime typo left in an edited file (baseline, not a regression)
- Location: `src/paa/tui/dashboard.py:347` (`strftime("%H:%:%S")`, and `:338` `%H:%:%M:%S`). · Evidence: [RAN
  test_ledger_with_events -> FAILED ValueError: Invalid format string; VERIFIED identical at dc06ed5 via `git
  show`]. Known category-B baseline (W3 disclosed "known strftime"). Out of FIX-01's write-set and correctly
  not "fixed" by widening the diff, but the file WAS in the write-set, so one line — the fix is to widen its
  write-set or carry it as a named task; silent coexistence plus an all-pass claim line is what made it look
  like a regression. No fix required for this wave; track explicitly.

### R-W4b-003 [MINOR] Lock-blind docs staleness remains (disclosed, correctly scoped out)
- Location: ~65 standalone non-blank orchestrator cites (DOCS2 FINDING 1, incl. stale-at-base class needing
  sentence-reading) + daemon/facts/dialect lock-blind staleness (DOCS FINDING) + `docs/modules/agents.md:59`
  backtick-pairing blindness (`:688`/`:737` invisible to the lock; `:737` target is blank). · Evidence: the
  reports enumerate them; my spot-reads confirm DOCS2's re-points are sentence-read, not shifted, and the
  agents.md:59 wrap is consistent with `find_references` yield rules. · Why MINOR: the lock — the gate's
  own contract — is green; the residue is "wrong but non-blank," the exact class W1 left (~200) and swept
  later. The prose-wrap fix exceeds a numbers-only mandate. · Recommended fix: one semantic-sweep task owning
  (a), (b), and the agents.md:59 wrap together with a docs-lock extractor hardening note.

### R-W4b-004 [MINOR] NEED must become a named task (v5-shape STRICT wedge)
- Location: `src/paa/storage/relational/migrations.py` v6 step order; NEED posted by FIX-01 (§7 + COMMS
  2026-11-07T18:52:00Z). · Evidence: §2 NEED assessment (correct scope call). · Why MINOR now: no live
  regression introduced by this wave; the wedge is pre-existing (P003-era) and requires a foreign-writer
  CHECK bypass. · Recommended fix: W5 task — a guarded SQLite pre-repair (`json_valid(aliases)` false ->
  rewrite `'[]'` + emit `migration.backfill_alias_corrupt`) before the column adds, with the PG probe
  decided in the same task; plus a one-line runbook note (pragma write then migrate). Do not let it rest
  without an owner.

### R-W4b-005 [MINOR] Claim guard's silent class (backends that raise programming errors read as "empty")
- Location: `src/paa/context/allocator.py:259-263`. · Evidence: read — `except Exception` maps everything,
  including `TypeError`/`AttributeError` from a mis-wired backend, to `underfed`. · Why MINOR: the warning
  line makes it observable, and the house posture is degrade-and-log; but a permanently mis-wired backend
  then presents as "no work" forever rather than erroring once loudly. · Recommended fix: none required this
  wave; optionally distinguish `StorageError`-family/queue errors from programming errors in the follow-up
  that closes R-W4-002 scheduling.

### R-W4b-006 [NIT] Docs fence nit
- DOCS2's `docs/modules/agents.md` vs `AGENTS.md` case-insensitivity note is host-specific; on a
  case-sensitive checkout the untracked twin could diverge. Cosmetic for the run record. No action.

## 5. Attempts to break it
1. **Graph insert on a colliding canonical name** (two ids, one name): facts' UNIQUE(canonical_name) still
   fires under the new INSERT (norm columns do not change the constraint set) — same failure class as before,
   handled by the existing `ConstraintViolation` mapping. Held (reasoned; no execution — substrate path
   unchanged).
2. **Graph rename of a facts-written entity**: canonical norm refreshes, aliases norm + raw preserved —
   aggregate stays consistent. Would fail if DO UPDATE had written `'[]'` (the rejected alternative); the
   dedicated rename test pins the chosen behaviour. Held by construction + test.
3. **Non-ASCII graph name** ("Éclair Project"): exact rung resolves, stored `éclair project`. The real fold,
   not `lower()` — a `lower()` backfill would store the same string for this input, so the test also relies
   on the exact-rung comparison shape; the dedicated P003 fold tests (İ/İZMİR) cover the distinguishing
   cases. Held.
4. **claim() raising a BaseException** (`KeyboardInterrupt`): propagates out of the guard (Exception-only
   catch) — correct for interpreter signals; break attempt failed (code held) in the intended direction.
   `RuntimeError` (simulated backend-down) degrades to `underfed` with the warning — intended.
5. **TUI with a settings fake lacking `.reflection`**: `getattr` chain renders disabled; no tick crash. Held
   by code reading + the disabled-default render test.
6. **Marker absent / stale** through the runner: unknown (None age) vs `9.2d (stale)` — both pinned by
   hand-built info tests and a real tmp marker. Held.
7. **Corrupt blob on PG** (v5 shape): ADD COLUMN does not re-validate CHECKs, so the backfill+count path
   fires there — count-log covered on PG by construction. NOT RUN (no DSN); CI must prove it. (No claim
   made beyond SQLite anywhere in this report.)
8. **Combined-order suite runs**: the honest break. `allocation` before `TestVectorSinkSemanticRoundTrip`
   fails deterministically through the pollution path — EXECUTED and FAILED (this is R-W4b-001, not a
   hypothetical). Reverse order, and gatherer-first collection, pass.
9. **Docs lock at HEAD**: [RAN -> pass]. The 65 lock-blind + invisible-:688/:737 class remains wrong-but-green
   by disclosure — attempted to verify invisibility independently is out of the lock's own capability; the
   wrap mechanism is verified by reading lines 58–59.
10. **Docs citation census** (DOCS2's "zero references to the 5 other shifted files"): spot-consistent with
   the 46-file stat (docs touch orchestrator lines only) — not re-enumerated independently; accepted as
   CHECKED-BY-READ, low risk either way (a missed citation would have shown as a lock failure at HEAD).

## 6. Dimension checklist
| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | PASS | Graph fold agreement, TUI mirror truth, claim degrade, count-log — all root-cause fixes; DO UPDATE deviation is the correct semantics; NEED deferral is the correct scope call |
| Root cause vs symptom | PASS | One normalisation point extended to the second writer; TUI now shares the one formatter; backfill honesty improved at the reachable layer |
| Regression / compat | PASS WITH MAJOR | C1–C5 confirmed non-weakened; pre-existing strftime correctly left alone; but the new test file regresses test isolation in a reproducible order (R-W4b-001) |
| Concurrency / idempotency | PASS | Single-statement upsert; claim guard idempotent (underfed no-op); backfill guards untouched |
| Error handling / degrade | PASS (MINOR memo) | Claim degrade is the asked-for posture; R-W4b-005 notes the silent-programming-error class |
| Security / abuse | PASS | ruff 0 (S608-clean); no new LIKE/SQL surface; error logs carry own-validation text only |
| Performance / scalability | DO-LATER | Backfill unbounded scan stands (O3); graph write is single-statement |
| Migration / rollback | PASS WITH NEED | Count-log is safe; v5-shape SQLite wedge is pre-existing and tracked as NEED (R-W4b-004), not introduced here |
| Observability | IMPROVED | TUI now truthful; count-log line exists; norm-NULL gauge still absent (O5 DO-LATER) |
| Maintainability / conventions | MINOR | TYPE_CHECKING reuse correct; no new block; test-stub violates order-independence (R-W4b-001); docs residue is sweep-class (R-W4b-003) |
| Docs impact | PASS (gate) / MINOR (semantics) | Lock green at HEAD; ~65 + invisible cites remain (R-W4b-003) |

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| O1 | `tests/agents/test_allocation_callsite_p007.py` `_enable` stub | Pair-order failure, §4 R-W4b-001 | Fixes a deterministic order-dependent suite failure | Small (import hoist or full-settings stub) | Low | DO NOW |
| O2 | `_backfill_entity_norm` | fetchall + per-row UPDATE, one transaction | Largest migration memory/lock hold at scale | Medium (chunk by id, keep guards) | Medium | DO LATER |
| O3 | norm-NULL startup probe | No count of un-normalised rows anywhere | Catches the next second writer early | Tiny | None | DO LATER |
| O4 | `run_allocation -> Any` typing | Already `-> AllocationResult` in this diff | — | — | — | NOT WORTH IT (closed) |
| O5 | v5 pre-repair step | STRICT wedge, §5 | Un-wedges v5+corrupt SQLite DBs | Small–medium | Medium (guard + PG probe) | DO LATER (see R-W4b-004) |

## 8. Cross-agent / cross-task interactions observed
- **P-006 ↔ P-007 (R-W4-002)**: still correctly deferred — `attention_top` stays honestly null until the W5
  scheduler lands; the fix wave did not smuggle a scheduler in. Tracked task, not silent residue.
- **P-003 ↔ graph substrate**: now closed at the substrate contract level (SQLiteGraphStore), but the same
  foreign-writer class remains discoverable only by a norm-NULL probe (O3) — C3b is the tripwire.
- **DOCS ↔ FIX-01 line shifts**: the two-stage DOCS pattern worked (FIX-DOCS left the orchestrator residual,
  DOCS2 cleared it at landed numbers); DOCS2's residual sweep items (65 + invisible cites) overlap DOCS's own
  daemon/facts/dialect lock-blind FINDING — one sweep task owns both.
- **TREV P1 (run_allocation callsite)**: closed by the new tests in substance; the pollution defect is the
  only gap on the closure, and it is test-side, not callsite-side.
- **NEED ↔ R-W4-008**: the wedge means the count-log's "migration completes" criterion holds for
  crash-mid-step + PG shapes only — the reports state this plainly, which is why this is a NEED and not a
  silent miss.

## 9. What I could not verify
- PostgreSQL shapes (v6 ALTER/backfill, parity, dialect PG predicates) — no `PAA_TEST_POSTGRES_DSN`; all
  PG-gated tests skipped in my runs too. CI must execute them before P-003 closes.
- Full `pytest tests/` suite (scoped only per standing orders + brief). The full-suite masking argument in
  R-W4b-001 is inferred from collection-import mechanics and reproduced in miniature; it is not a full-suite
  run, so the masking is asserted as highly likely, not proven here.
- The exact count of ~65 lock-blind cites and the `find_references` invisibility of agents.md:59 — accepted
  from DOCS2's enumeration + my line reads; I did not re-enumerate the 127 orchestrator refs independently.
- Whether `allocate_attention`'s module-level wrapper (called by `run_allocation`) has other callers whose
  claim-failure posture changed: grep shows `run_allocation` still has zero callers (R-W4-002), so the
  guard's blast radius is future-scheduler-only.
- Docs lock semantics beyond my 18 spot-reads (I read ≥8 per brief; full semantic re-check of all 113
  re-points is DOCS2's own work, here spot-verified and lock-verified only).
