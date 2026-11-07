<!-- ORCH-DIGEST:START -->
agent: TREV-W3 | scope: wave 3 (T-009c/T-010/T-006/T-004b) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 79 new/updated (tier 21, dual-vault 35, health 2, counts 3, dashboard 5, coder fallback 11 incl. dataclass test, e2e 1 rewritten + coder stale 1 updated)
fail-before/pass-after confirmed: 4 / reasoned: 2 / not-possible: 0
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 3
flaky_suspects: none
missing_edge_cases: P1 1 | P2 6 | P3 6
mutation_survivors: 1 of 14 plausible bugs not caught (panel-builder happy path)
changed_behavior_without_tests: 3 of 4 runner panel builders have no positive-path test (decision_tracker, autonomy awaiting_ids, agent_monitor recent_roles)
confidence: high (all 7 W3 test files executed; only failure is baseline category-B strftime, reproduced and confirmed pre-existing)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W3 (independent, read-only; did not write this code)

## 0. Method

Blind pass first: read `f31fb4f..HEAD` diff + all seven W3 test files BEFORE opening
implementer reports, then claims audit. Ran the W3 files myself:

- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_tier_sweeper_p009.py tests/memory/test_dual_vault_p010.py tests/api/test_health_p006.py tests/memory/test_counts_p006.py tests/tui/test_dashboard.py tests/coder/test_runtime_tools.py tests/memory/test_e2e_remember_recall.py -> 1 failed, 162 passed`]
- The single failure is `tests/tui/test_dashboard.py::test_ledger_with_events`, `ValueError: Invalid format string` at `dashboard.py:346` `"%H:%:%S"` — baseline category-B (00-mission.md:33), verbatim. [RAN `pytest tests/tui/test_dashboard.py::test_ledger_with_events -> same ValueError`]
- [RAN the four new-file suites again separately -> 61 passed (21+35+2+3, matches file counts exactly)]
- [RAN coder + e2e files -> exit 0, all passed]
- No temp worktree used (read-only reviewer cannot revert; fail-before assessed by construction + implementer revert evidence where they ran it).
- Isolation verified: `tests/conftest.py` `paa_home`/`settings` (tmp) [VERIFIED tests/conftest.py:23-52]; `db`/`ledger` function-scoped tmp fixtures; dual-vault and vector stores under `tmp_path`; all W3 tests isolated. No shared-DB use.

## 1. Per test/suite assessment

| Test/suite | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| tier `TestT10Move` (8) | Yes — order spy, boundary, final rung | Order/boundary/drop-dedupe mutations all caught (see §3) | Yes, EPOCH-relative offsets | Spy calls through | Strongest suite in wave. `recording_execute` observes ledger count (1) at UPDATE time — proves event-before-move, not just final state |
| tier `TestDryRun` (2) | Yes — zero events + zero row mutations incl. `tier_updated_at`; TypeError pins no-default ctor | dry_run=False hardcode fails both | Yes | No | `test_dry_run_has_no_default` is a contract pin, good |
| tier `TestT10bRebuild` | Yes — fold == live + `replay` unknown==[] + `verify_chain` | Wrong event type/payload fails | Yes | No | Totality check present |
| tier `TestIdempotency` (2), `TestCrashBetween` | Yes — dedupe by full key; crash→recover completes move | Remove discriminator fails | Yes | Crash via monkeypatched `execute` raising — failure injection, calls-through otherwise. Legit |
| tier `TestUnionPrecedence` (2), `TestCuratorSeam` (2) | Yes — both halves of union; seam dry default + live injection | Removing tier call from curator tail fails seam tests | Yes | No | The `or`-composition of curator dry-run flags is covered both ways (see §3) |
| tier `TestObservability`, `TestNullStampFallback`, batching | Yes | — | Yes | structlog capture is fine | — |
| dual-vault `TestEnumSet` (6), `TestGovernancePath` (3), `TestMaterialization` (3) | Yes — exact 15-union, no-removal, traversal rejection, no-seed-on-construct | Revert fails imports | Yes, tmp_path | No | `test_governance_files_are_not_enum_members` pins ADR placement decision |
| dual-vault `TestFenceConflict` (4) | Yes — inside-edit raises named path + human edit stands + v2 not landed; outside merges; adopt-then-detect; corrupt record rebuilt | Removing `_check_conflict` fails test 1 | Yes | No | Fence-overwrite mutation caught. ValueError-subclass contract asserted both ways |
| dual-vault `TestIdempotencyAndCrash` (2) | Yes — byte-identical double write; `os.replace` crash leaves old bytes, no tmp residue, next write clean | — | Yes | `os.replace` patched to always-boom: OK failure injection | Crash-between-record-and-file window (false-conflict recovery) NOT tested — §6 P2 |
| dual-vault `TestMarkerInjection` (4), `TestSourceHeader` (2), `TestSyncGate` (3) | Yes — dup/dangling markers, content-barried markers (no file created), source header, disabled = untouched bytes | Hygiene removal fails | Yes | No | Strong hostile-input coverage |
| dual-vault `TestSingleWriter` (2) | Structural grep: `def _write_managed_block` absent from all of src | Restoring the duplicate writer fails | Deterministic (path resolution) | No | Fragile-ish (reads every src .py) but correct as a lock |
| dual-vault `TestReflectionDelegation` (4) | Yes — accumulates through WorldModel, preserves human text, inherits conflict, rejects non-playbook filename | Deleting delegation fails all 4 | Yes | No | `filename` closed-set tightening pinned |
| dual-vault `TestAntiGoalsAgreement` (2) | Pins known FINDING-2 behavior (marker string lands in goals list) | Fence-aware reader fix would fail test 2 *deliberately* | Uses isolated `settings` (tmp PAA_HOME) | No | Deliberate pin of defective behavior; documented in report. Acceptable, flagged below |
| health `TestHealthPanels` (2) | Yes — additive keys, old keys intact, `attention_top is None`, isolation → 200 with panel null | Revert schemas/runner fails | Yes (`client` fixture) | Monkeypatch of `FactRepository.count_facts_by_domain` = failure injection on collaborator. Legit | Only counts-raising path tested; other panels' raising paths untested (P3) |
| counts `TestGroupedCounts` (3) | Yes — empty zeros, domain+status rollup, superseded default-excluded/flag-included | Removing superseded filter fails | Yes | No | PG dialect untested (SQLite only) — acknowledged NOT-RUN |
| dashboard appended (5) | Yes — 5× No data yet, populated ranked rows + order, partial-null, hostile markup literal, builder isolation | Revert snapshot/runner/dashboard fails | Pure renders | fakes are ledger/db doubles, not the render unit | Dict-form `attention_top` + scalar/malformed branches untested (P2); builder fake has empty heads so positive paths untested (P1) |
| coder `TestRecallVectorFallback` (10) + dataclass test | Yes — present-half count+touch, degrade halves, keyword-first 0-embed calls, NUL 0-calls, failure degrade, derived-vs-snapshot confidence, superseded skip, limit clamp | Removing tier call → 3 fail with `0==1` (implementer RUN, see §5) | Yes, tmp store, synonym embedder, finally-close | `_Boom*` fakes target the vector backend, not the unit. Legit | `huge-limit` assertion weak (§5); get_fact-raising hydrate path untested (P2) |
| e2e rewritten (1) + coder stale (1) | Yes — two halves: present count 1 + touch + verbatim note; absent count 0 + same note | keyword-only revert fails present half | Yes | No | Note pinned by `==` equality, not substring. Correct deliberate update of category-D/DECISION T-007 |

## 2. Coverage of changed lines/branches, error paths, boundaries

- **Tier sweeper**: all branches covered except concurrent-tick guard (`WHERE tier=?` no-op under concurrent move) and per-call `sweep(dry_run=True)` override on a live sweeper. Boundary pair (exact 4h not due / 4h+1s due) present. Empty table, unknown domain, immutable, T4-never-due, NULL-stamp fallback, batching all present. Error path (unknown-domain count) present.
- **Dual vault**: conflict/no-conflict/corrupt-record/marker-hygiene/content-injection/sync-off all covered. Uncovered: crash-between-file-commit-and-record-write recovery (documented false-conflict path has no test); concurrent writers (documented non-goal).
- **Counts**: empty, grouped, superseded both ways. PG dialect uncovered.
- **Health**: additive shape + one isolation path. Other four panels' failure paths uncovered.
- **Dashboard render**: null/populated/partial/hostile/isolated covered. Dict-rank branch of `attention_top`, scalar fallback branches, `_counts_table` non-numeric sort key uncovered.
- **Runner builders**: `_memory_browser_counts` covered (via counts + health total). `_decision_tracker`, `_autonomy_state` (awaiting count), `_agent_monitor` (recent_roles) have NO positive-path test — only empty-head fakes returning empty. This is the wave's one significant coverage hole (P1).
- **Vector fallback**: present/absent/NUL/limit/failure/superseded/derived all covered. Hydration `get_fact` raising (loop sits outside the `try`) uncovered; multi-fact limit truncation uncovered; Qdrant backend uncovered (acknowledged).

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| Event after row move (order swap) | Yes | tier spy `seen_at_update == [1]` |
| dry_run bypass (hardcode False / ignore flag) | Yes | dry-run zero-write + curator seam dry-default; `or`→`and` flip in curator tail caught by seam test 1 |
| Fence silent overwrite (drop `_check_conflict`) | Yes | inside-edit raises + human edit stands + v2 absent |
| Fake attention score (compute instead of None) | Yes | health `attention_top is None` + builder `attention_top is None` |
| Second touch copy added | Not by W3 files — by W2 `tests/memory/test_touch_unification.py` AST lock (outside diff; implementer re-verified 9 pass) | W2 lock, not new |
| Boundary flip `>` → `>=` | Yes | exact-4h-not-due test |
| Drop ADR-0008 discriminator dedupe | Yes | double-emit + sweep-twice idempotency |
| Cascade instead of one-rung-per-tick | Yes (T10b fold would land T4, not T1) | fold-equals-live `{due_a: T1, due_b: T4}` |
| Unknown domain silently moved | Yes | errors==1 + tier unchanged |
| Remove superseded-skip in vector tier | Yes | `test_superseded_vector_hit_is_skipped` |
| Snapshot confidence instead of derived | Yes | derived-confidence test (approx `effective_confidence` + `< snapshot` + `< 0.1×initial`) |
| Vector-first instead of keyword-first | Yes | counting-embedder 0-calls test |
| Remove marker hygiene / content-marker rejection | Yes | duplicate-BEGIN, dangling-BEGIN, content-carries-markers tests |
| Remove superseded filter in counts | Yes | superseded test |
| Panel builder raising kills response/tick | Yes | health isolation + builder isolation tests |
| Markup crash on hostile strings | Yes | literal `[red]hacked[/red]` assertion (also pins that markup is NOT stripped) |
| Happy-path panel builders broken (policy decision never surfaces, awaiting count always 0, recent_roles always []) | **NO — survives** | No test with non-empty heads. Mutation survivor. P1. |
| `get_fact` raising during hydration propagates | No — untested behavior (arguably fine, keyword path would fail too) | P2 |

## 4. Flakiness evidence (repeated runs) and test-suite health

- [RAN 7-file scoped run once + 4-file rerun + coder/e2e rerun — all green except the one pre-existing strftime failure, reproduced identically each time]. No flaky suspects.
- No sleeps, no wall-clock dependences (all tier timing is EPOCH-relative), no shared DB, no fixed ports. Module-level `EPOCH = utc_now()` is safe because every assertion is offset-relative and fixtures are function-scoped.
- Suite health: no slow/order-dependent/shared-state indicators observed in the scoped run (12.78s for 163 tests).

## 5. Fail-before/pass-after audit (implementer claims)

- **W3-IMPL-01 (tier)**: claims fail-before = collection `ModuleNotFoundError` with `tier.py` absent [reasoned-CONFIRMED: `tier.py` is new in diff; test imports it at module scope, so pre-fix collection necessarily errors]. Pass-after [RAN by me: 21 pass]. Mutation claims (order spy, `>`/`>=`, dedupe) verified by reading tests against source — each maps to a concrete assertion. Cursor: tier.py reports "96 passed incl. backstop" — consistent with my scoped runs.
- **W3-IMPL-02 (dual vault)**: claims worktree fail-before = `ImportError: cannot import name 'VaultConflictError'` [reasoned-CONFIRMED: new symbols + new file]. Pass-after [RAN by me: 35 pass]. Note: worktree fail-before was run by implementer, not re-runnable by me read-only — accepted as construction-necessary.
- **W3-IMPL-03 (P006)**: claims fail-before "symbolic" (TypeError on Snapshot kwargs / AttributeError / missing health keys) at HEAD [reasoned-CONFIRMED via `git show HEAD` absence — I verified `attention_top`/`count_facts_by_domain` don't exist in the f31fb4f tree via the diff]. Pass-after [RAN by me: 10/10 new pass in my runs]. The API-suite harness hang claim (§6) I did NOT reproduce — `tests/api/test_health_p006.py` ran and exited cleanly in my runs; reclassify as possibly transient rather than a standing fact.
- **W3-IMPL-04 (vector fallback)**: claims fail-before RUN by reverting the tier call → 3 failed with `assert 0 == 1` (behavioral, not TypeError), then restored → 11 pass [accepted: specific, mechanism-consistent; I cannot re-run without editing]. Pass-after [RAN by me: coder + e2e exit 0]. Degrade-halves-pass-without-tier correctly acknowledged by implementer (they pin the keyword-only contract, not the fix).
- **Stale-test updates**: `test_remember_then_recall_round_trips` (category-D) — old assertion `"semantic recall unavailable"` substring; replaced with verbatim `==` on the new note. This is a strengthening, not a weakening. The e2e rewrite similarly tightens (count 0 single-half → count 1 present + count 0 absent + verbatim note). Both justified in test comments. No test was deleted or weakened.

## 6. Findings

- **R-W3-001 (MINOR)** `tests/coder/test_runtime_tools.py` `test_vector_limit_is_bounded_not_untrusted`: the `limit=1000 → count <= 50` half is near-vacuous — with only one seeded fact, an unclamped search also returns 1. Recommend a two-fact variant asserting `limit=1` truncates to 1 when 2 facts exist, so clamping is actually discriminated. Location: coder fallback tests.
- **R-W3-002 (MINOR)** `tests/memory/test_dual_vault_p010.py` `test_managed_block_injection_pinned_as_finding`: deliberately pins the FINDING-2 defect (fence-unaware `_load_anti_goals` ingesting markers). Documented and intentional — no action, but the next fence-aware fix must update this test *deliberately*, and the test name/docstring should say so louder to prevent a future reader calling it a regression. Already partially done; NIT-level.
- **R-W3-003 (MINOR)** `tests/tui/test_dashboard.py` `test_p006_populated_panels_render_rows`: asserts `out.count("No data yet") == 0` — good — but the dict-form `attention_top` rank branch (`dashboard.py` `_panel_attention` dict case, sort by score desc) and scalar/malformed fallback branches have zero tests. Either add one dict-form test or mark the branches as defensive-only in a comment.
- **R-W3-004 (MINOR)** `src/paa/tui/runner.py` `build_panel_fields` docstring claims per-panel isolation, but `_agent_monitor` reads full `read_correlation` per lineage then takes `[-1]` (implementer self-flagged §3). Test does not pin the behavior either way. Not a test defect per se; noted so the gate doesn't mistake "tested" for "efficient". No fix requested.
- **R-W3-005 (NIT)** `tests/memory/test_tier_sweeper_p009.py`: `test_dry_run_has_no_default` uses `# type: ignore[call-arg]` — acceptable and deliberate (pins the TypeError). No action.
- **R-W3-006 (NIT)** W3-IMPL-03 report §6 API-hang claim not reproduced by me; health file exits cleanly in my environment. Downgrade that claim to transient.
- **R-W3-007 (NIT)** `tests/api/test_health_p006.py`: isolation test monkeypatches `FactRepository.count_facts_by_domain` on the class — fine — but `_memory_browser_counts` constructs `FactRepository(rt.db)` per call; no test pins the `rt.db is None` → `RuntimeError` → isolated-None path. Trivial; optional.

## 7. Missing edge cases and tests (prioritized)

**P1 (must add)**
1. **Panel-builder happy paths** (`tests/tui/test_dashboard.py` or a small `tests/tui/test_runner_panels.py`): drive `build_panel_fields` with fake ledger heads that carry real events — assert `decision_tracker` surfaces `policy_decision` + reason, `autonomy_state` counts `AWAITING_HUMAN` projections into `awaiting_ids`, `agent_monitor` collects `recent_roles`. Scenario table: 2 open lineages (one AWAITING_HUMAN with a POLICY decision, one EXECUTING), assert all three panels non-empty and correctly shaped. Currently a builder that returned `[]`/`0` forever would pass every W3 test (mutation survivor, §3).

**P2 (should add)**
1. **Curator tier-sweep failure**: `_phase_decay` has no try/except around `tier_sweeper.sweep`; no test pins whether a raising tier sweep kills `run_maintenance`. Add a test with an exploding tier sweeper injected, asserting the decided behavior (complete or fail) so it can't drift silently.
2. **Per-call `sweep(dry_run=True)` override** on a live `TierSweeper` (constructor False, call True) — the curator's preview composition path is only half-covered (constructor-flag tests exist; explicit-override does not).
3. **Vault record-window recovery**: simulate crash after document commit before `_record_hash` (e.g., patch `_record_hash` to raise after a write), then assert the next write raises the documented false-conflict and that deleting the record recovers. Currently the most safety-relevant design decision in P-010 has no test.
4. **Vector hydration DB failure**: `get_fact` raising inside `_vector_fallback` (outside the try) propagates out of `run()`. Pin the decided behavior with a test (either degrade or propagate as documented).
5. **Invalid tier value in row** (`tier='TX'`): `TierPolicy.resolve` presumably normalizes; no test pins what the sweeper does with a stored out-of-ladder tier. Pin it (move from resolved tier, or error-counted).
6. **Dashboard dict-form `attention_top`** ranking by score descending (see R-W3-003).

**P3 (nice to have)**
1. Postgres dialect run for `count_facts_by_domain` GROUP BY (needs live server; CI-only, acknowledged NOT-RUN).
2. Multi-fact vector limit truncation (see R-W3-001).
3. `apply_to_playbook` with `vault is None` silent return (behavior unpinned).
4. Concurrent-writer vault test (documented non-goal; would need a lock design first).
5. Health isolation for autonomy/agent panels raising (only counts covered).
6. Qdrant-backed fallback test (only numpy exercised; tool only reads `hit.id`, so risk is low).

## 8. What I could not verify

- Fail-before by actual revert (read-only role; no source/test edits permitted) — assessed by construction + implementer revert evidence instead. The vector-fallback revert result (3 × `0 == 1`) is accepted on implementer's report, not re-run.
- Real `Runtime.build` wiring populates `Runtime.embedder` with the sink's object (needs a live build; implementer honestly marks ASSUMED/NOT-RUN).
- True minimal-install import (no numpy/qdrant/sentence-transformers) — implementer ran a simulated sys.modules block only; I did not repeat it.
- Postgres/S3/gated suites, container paths, full `pytest tests/` — out of scoped-check scope per STANDING-ORDERS §16.
- The two stale-test updates' note string byte-identity is established by [RAN pass], not by my independent diff of the string literals — acceptable since equality assertions ran green.
- Full-suite regression beyond the 7 W3 files (scoped run only).
