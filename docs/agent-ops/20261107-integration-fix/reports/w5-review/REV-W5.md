<!-- ORCH-DIGEST:START -->
agent: REV-W5 | scope: wave 5 (T-008, T-011, T-007b, W5-DOCS) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 3 | MINOR 4 | NIT 3
claims_audit: 14 claims checked / 2 unsupported or false
band_aids_found: none
top_3_risks: production DRAIN_THROTTLED unwired (runtime.py:508); cost-gate PG cursor breaks; attention_top stale/dup
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 0
reversal_check: done (reasoned + test fakes read; no reverts executed)
confidence: med (blind pass complete; claims audit done — 14 checked, 2 false; PG/concurrency paths reasoned, not executed)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W5 (independent, read-only)

## 0. Method
Diff range reviewed: `1d7c5bf..HEAD` (f06b5bc T-007b, 6e1819a coding pilot, 3a5da25 cost gate, 1268d54 docs+ADR-0044, + report commits). Files read in context: metrics.py cost fn + guards, curator.py _phase_analyze + run_maintenance, orchestrator.py mediate_delegation/run_allocation/_emit, delegation.py propose, specialist coding.py + __init__, daemon.py loops + tick, creator.py drain + emitter, runtime.py _build_memory_creator/_build_agents/call sites, tui/runner.py _attention_top/build_panel_fields, config.py CostGate/Allocator/Coding settings, allocator.py extract/emit, store.py open_correlations, ADR-0040/0041/0036/0044, both DDL files (fact id type). Commands run: ruff (0), mypy (3 baseline), W5 scoped tests (216 pass — see §9 for exact), tests/tui (1 pre-existing strftime fail), docs lock (5 passed), import conventions (21 passed), allocation-callsite + creator (54 passed). Order followed: blind pass first, then implementer reports (§2 follows).

## 1. Blind-pass findings (before reading implementer reports)

### R-W5-001 [MAJOR] Production DRAIN_THROTTLED is never emitted — creator wired, runtime not
- Location: `src/paa/runtime.py:508` (`MemoryCreator(db, extractor=..., embedding_sink=...)` — no `ledger=`). Evidence: creator accepts `ledger=` (creator.py:1127) and emits on both breach sites (creator.py:1576/1596), but the only production construction path `_build_memory_creator` never passes it; `ledger` is in scope at the call site (runtime.py:364). So in production the throttle stays log-only and R-W4-006 is not closed live. Creator-side emit is correct, tested with fakes, degrade-safe (emitter swallows failures). Fix (one line + kwarg): `_build_memory_creator(..., ledger=ledger)` threaded from runtime.py:346. Root cause is a wiring omission at the single production construction site, not the creator.

### R-W5-002 [MAJOR] Cost-gate cursor pagination breaks on PostgreSQL (`id > ''` vs UUID id)
- Location: `src/paa/memory/curator.py:477,482` (`cursor = ""`, `WHERE ... AND id > ?`). Evidence: SQLite `hot_serving_active_facts.id` is TEXT (schema_sqlite.sql:272) so `""` works; Postgres `id` is UUID (schema_postgres.sql:230) so `''` is not a valid uuid param — the first page raises, `_phase_analyze` fails, run_maintenance's per-phase handler counts an error and the gate silently never runs on the PG substrate. All four cost-gate tests are SQLite. Fix: first page without the id predicate (or a NULL-sentinel branch), keeping the total-order pagination for later pages.

### R-W5-003 [MAJOR] `attention_top` can show stale, duplicated decisions and drops terminal lineages
- Location: `src/paa/tui/runner.py:171-219`. Evidence: scans `open_correlations()` ordered `updated_at ASC` capped at 50 (store.py:355-358) — the *oldest* open lineages, not the freshest; accumulates every `ATTENTION_ALLOCATED` event in those lineages with no recency bound and no dedupe by `item_id`, so the same item re-allocated on later ticks appears multiple times and old decisions outrank nothing but persist forever; lineages that went terminal after allocation vanish from the panel. Standalone allocator lineages (uuid5(item_id), allocator.py:182) are non-terminal so they do show — but only while inside the oldest-50 window. Tests use a single fresh lineage with a TASK_REQUESTED head, masking all three properties. Empty→None is correct and pinned. Fix: dedupe by item_id keeping the latest score, and/or scan most-recent-first; document the terminal-lineage drop.

### R-W5-004 [MINOR] DRAIN_THROTTLED discriminator derives from process-relative monotonic time
- Location: `src/paa/memory/creator.py` `_emit_drain_throttled_event` (`int(window_started * 1000)`, `window_started = loop.time()`). After a restart, early windows produce near-identical discriminator values to a previous process's, so a genuinely new breach can dedupe against a pre-restart event (store returns existing). Two processes sharing one ledger dedupe each other the same way. Fix: wall-clock window epoch (or a boot/process id in the discriminator).

### R-W5-005 [MINOR] Specialist policy-coverage test cannot fail (tautology); read-only half unpinned
- Location: `tests/agents/test_coding_specialist_p011.py` `test_every_mutating_tool_is_covered_by_policy`. The test defines the mutating set as (glob-matched ∪ MUTATING_RUNTIME_TOOLS) and then asserts each member is glob-matched or in the tuple — `A or B ⊢ B or A`, vacuous. A future mutating tool in CODING_TOOL_NAMES matching neither would pass silently, exactly the read-only-by-accident class AGENTS.md warns about. The underlying property holds today (verified: exactly submit_task/run_task/remember_fact are mutating, all in MUTATING_RUNTIME_TOOLS, none glob-matched) — but the pin is decorative. Separately, nothing pins the read-only half == READ_ONLY_TOOLS (verified equal today by direct import). Fix: compute mutating from an independent source (e.g. the coder tool registry descriptions) or assert CODING_TOOL_NAMES ⊆ READ_ONLY_TOOLS ∪ MUTATING_RUNTIME_TOOLS and that each mutating tool is in the build tuple.

### R-W5-006 [MINOR] Allocation tick gate and allocator gate read different settings sources
- Location: `src/paa/daemon.py:293-295` (`self._rt.settings.allocator.enabled`) vs `src/paa/agents/orchestrator.py:1165-1171` (`get_settings().allocator`). When a Runtime is built with a non-global Settings object the daemon gate and the allocator's own gate can disagree (daemon passes → outcome "disabled" logged as a completed pass, or daemon blocks a globally-enabled loop). Same pre-existing pattern as the tier sweeper; the W5 test masks it with a FakeOrchestrator. Fix: thread the allocator settings through run_allocation instead of re-reading the global.

### R-W5-007 [MINOR] Cost gate is unbounded per pass (suggestions list + one log line per fact)
- Location: `src/paa/memory/curator.py:505-517`. With derivations, a large fraction of facts crosses threshold 1.0 (e.g. imp=conf=1, N=1 → total 2.0), so every maintenance pass appends O(N) suggestions and emits O(N) `cost_gate.suggestions` lines. Advisory-only so correctness holds, but at 100x facts this is log flooding + report-list growth. Fix: cap suggestions (count + top-K) or log the count once per pass.

### R-W5-008 [NIT] Cost gate reads the global singleton, not injected settings
- Location: `src/paa/memory/curator.py:458-460`. `MemoryCurator` accepts injected `MemorySettings` but cost-gate enable/threshold come from `get_settings()` — a custom-settings Runtime silently uses global cost-gate config. Pre-existing pattern (tier sweeper). Fix when curator is next touched: accept a CostGateSettings injection.

### R-W5-009 [NIT] Recursion-block emit is unguarded and its discriminator does not name the block kind
- Location: `src/paa/agents/orchestrator.py:880-896`. If the ledger append raises, `mediate_delegation` raises instead of returning the failure (pre-existing submit() shares the posture, so consistent). Discriminator `delegation-block:{sender}->{target}:depth{d}` collapses depth/cycle/node blocks with the same sender/target/depth into one idempotency key — a later *different-kind* block at the same depth is suppressed as a redelivery. ADR-0041 requires all three kinds to emit, so the collapse is observable. Fix (optional): include a block-kind tag in the discriminator.

### R-W5-010 [NIT] `test_coding_never_delegates_out` never delegates
- Location: test file bottom. The parametrised `depth` is unused; the test only asserts `can_delegate is False`. The property is real (coding.py class attr) but the test name overclaims an attempted-delegation check. Cosmetic.

### D-FORMULA verification (reviewer-confirmed, per W5-IMPL-01 request)
ADR-0040 names no equation, requires `utility=0 → inf` with the raw form never evaluated, and a positive threshold scale. The implemented ratio `(storage+retrieval+noise+maintenance)/utility` is the only shape satisfying both: an additive `sum − utility` cannot yield `inf` at utility 0, and an epsilon guard is explicitly rejected by the ADR. Threshold reads as "cost per unit utility" — coherent. **Confirmed sound.**
### D-DERIVATIONS verification
All five terms derive from stored columns with named derivations (storage=row share 1/N; retrieval=1/(1+use_count); noise=1−importance; maintenance=1−initial_confidence; utility=importance·initial_confidence); no silent 0/1 substitution, satisfying ADR-0040's source-gap rule. Maintenance uses 1−confidence rather than the ADR's suggested eviction-count adjacency — an acceptable named derivation, noted. Out-of-range stored values (importance>1, negative use_count, NaN) raise inside the try and are skipped with `report.errors += 1` — degrade-safe. **Confirmed sound.**

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| W5-IMPL-01: D-FORMULA ratio is the only qualifying shape | YES | ADR-0040 names no equation; additive cannot yield inf at utility=0; epsilon guard rejected by ADR:38-42. Reviewer confirms sound. |
| W5-IMPL-01: D-DERIVATIONS close source gaps, no silent 0/1 | YES | Five named transforms of stored columns; storage=row share (ADR-named). Maintenance = 1−confidence (named derivation; ADR suggested eviction-count adjacency — acceptable variant). |
| W5-IMPL-01: never deletes; fact stays hot | YES | Gate block has zero DELETE/UPDATE; tests assert row remains + counts unchanged [RAN green]. |
| W5-IMPL-01: disabled = zero cost calls; dry_run still suggests | YES | Code read + spy test green; dry_run decision documented in code. |
| W5-IMPL-01: 27 tests (18+9), ruff 0, mypy 3 | YES | [RAN] combined W5 files exit 0; ruff 0; mypy 3 baseline. |
| W5-IMPL-01: keyset pagination safe (ORDER BY id) | PARTIAL | Safe on SQLite (TEXT id); breaks on PG (UUID id, `id > ''`) — R-W5-002. Implementer did not consider the PG substrate. |
| W5-IMPL-02: 21 tests, fail-before 18F/3P | YES (pass-after [RAN]) | Fail-before not re-executed by reviewer; pass-after confirmed in combined run. |
| W5-IMPL-02: "adding an uncovered mutating tool fails the policy-coverage test" | FALSE | Test is tautological (R-W5-005); an uncovered mutating tool would pass. Property holds today by audit, not by the test. |
| W5-IMPL-02: tool execution deferred post-gate; seam not CoderAgent.build | YES | Disclosed both in report and coding.py docstring; ADR-0041 cost list supports scope. No stub. |
| W5-IMPL-02: emit before failure return; idempotent once | YES | Code read orchestrator.py:880-897; replay ROLLED_BACK + rollback_count=1 tested [RAN]. |
| W5-IMPL-03: daemon tick gated/supervised/lock-ordered | YES | Code read daemon.py:266-309; disabled test pins zero queue contact [RAN]. "Through the real allocator" overstates slightly: tests use FakeOrchestrator wrapping the real allocator — daemon→orchestrator.run_allocation link itself is only covered by W4's callsite suite (54 tests [RAN] with creator). |
| W5-IMPL-03: DRAIN_THROTTLED breach-only + dedupe + log-only fallback | YES | Code read + tests green; production activation honestly flagged PARTIAL (NEED posted) — matches R-W5-001. |
| W5-IMPL-03: attention_top live + empty→None; operator-fed, no fabricated producer | PARTIAL | Empty→None and no-producer verified. "Live" overclaimed: stale/dup/terminal-drop (R-W5-003) untried by their test. |
| W5-DOCS: ~120 re-points semantic (never blind +N); lock 5 passed; ADR-0044 filed+indexed | YES | Lock [RAN] 5 passed; sampled cites (ADR-0029, ADR-0044, mediate_delegation:837) hold at HEAD; 3 false-holders + persistence:178 best-holder flagged honestly. |

14 claims checked; 2 unsupported/false (policy-coverage pin; "live"/"real-path" overstatements), 2 partial. D-FORMULA/D-DERIVATIONS: reviewer confirms both decisions sound.

## 3. Long-run test (independent answers)
- **Source:** Formula/derivations re-derived from ADR-0040 text, not implementer summaries — both sound (see §1). Rollback semantics re-derived from replay.py/TERMINAL_EVENTS (resumable ✓). Specialist scope re-derived from ADR-0041 cost list (routing/guards/emit only ✓; tool execution deferral is in-scope scoping, not a stub).
- **Load (100x):** Cost gate O(N) scan every maintenance pass with O(N) suggestions + log lines (R-W5-007) — at 100x facts the log flood is the cost, not CPU. Allocation tick bounded (25/pass, 60s) ✓. Block-emit one append per blocked delegation, same cost class as lifecycle events ✓. attention_top bounded at 50 lineages but reads full lineages per 2s tick — O(total events) per tick, pre-existing panel shape.
- **Hostile input:** Cost pure fn rejects negative/NaN/inf (ValueError); DB-hostile rows skipped per-row ✓. Allocator upstream poisoning untouched ✓. Specialist: empty/malformed payloads degrade; model raise → failure result ✓. Panel skips non-dict/non-numeric payloads ✓. Score NaN in a ledger payload would sort nondeterministically (no finite check in runner) — reasoned, allocator only writes guarded scores.
- **Operability:** Cost gate rollback = flag off, nothing stored ✓. Specialist rollback = enabled=False ✓. Allocation loop rollback = flag off ✓. DRAIN emit rollback = omit ledger handle (current production state) ✓. Observability: cost suggestions log-only (no ledger event — ADR-0040 compliant); drain breaches log + (pending wiring) event; allocation outcome logged per tick ✓.
- **Reversal:** Removing `total_memory_cost` breaks 27 tests at import/call ✓ (read-implied). Removing the rollback emit breaks 3 specialist tests (implementer-verified + code-read). Removing `_run_allocation_tick` breaks scheduling tests ✓. Deleting `_attention_top` breaks the ranked test ✓. Docs sweep reversal: re-breaks ~120 semantic cites (lock alone would not catch it — semantic correctness rests on the sweep table, verified by sampling).

## 4. Findings
Blind findings R-W5-001…R-W5-010 above (§1) stand; claims audit added no new defect beyond R-W5-005 (false pin claim) and the "live panel"/"real path" overstatements (R-W5-003/R-W5-006). Severity re-check: R-W5-001 stays MAJOR (disclosed NEED, one-line fix, no data loss; blocks the wave-DONE claim for T-007b production activation but not merge). No BLOCKER: no data-loss, no auto-delete, no security hole, no silent corruption found in the diff.

## 5. Attempts to break it
- Cost gate: reasoned PG cursor break (R-W5-002); out-of-range importance/confidence/use_count → caught, skipped, errors+1 (read try/except at curator.py:489-504); threshold validated gt=0 (config.py:730); disabled path zero cost calls (code read curator.py:461). Never-deletes verified by read: ANALYZE block only appends to report list + log.info; no DELETE/UPDATE/quarantine in the gate block.
- Specialist: read mediate_delegation — emit precedes `return AgentResult.failure(exc)` ✓ (orchestrator.py:880-897). Cycle probe: self-delegation hits `would_cycle` sender==target ✓ (delegation.py:70). Node-ceiling blocks also emit (propose raises with depth/ceiling set) — matches ADR-0041's "depth, cycle, or node count". Model raise degrades to failure result via base.run (base.py:355-363). can_delegate=False blocks out-delegation.
- Allocator tick: disabled → None without touching queue ✓ (daemon.py:294-295, tests); lock shared with maintenance+reflection ✓ (same `_curation_lock`); supervisor restarts loop on raise ✓.
- attention_top: empty → None ✓; fake-score fabrication absent ✓ (reads real events only); stale/dup properties per R-W5-003.
- NOT RUN live: Postgres cursor break (no PG server), daemon under real Runtime (would need shared env), Docker paths. [add any further attempts]

## 6. Dimension checklist
- Correctness: PASS with R-W5-002 (PG cost-gate) and R-W5-003 (panel staleness) as the two real correctness defects; formula/derivations/delegation/emit all sound.
- Root cause vs symptom: PASS — D-FORMULA/D-DERIVATIONS close ADR-0040's named gaps rather than papering them; allocator tick fixes the scheduling root cause (R-W4-002), not the symptom; drain emit fills the emitter root cause creator-side (wiring deferred, disclosed).
- Regression/compat: PASS — EventType set unchanged (diff-checked); AgentRole additive; creator kwarg optional (existing constructions unchanged); disabled-defaults inert; delegation/replay/coder suites green [RAN].
- Concurrency/idempotency: PASS with R-W5-004 (discriminator restart collision) and R-W5-009 (block-kind collapse) noted; allocation tick serialized on the shared curation lock; drain dedupe + rollback dedupe tested.
- Error handling: PASS — degrade posture consistent (emitter swallows, per-row skip, panel isolation, model-raise→failure). Unguarded block-emit raise noted (R-W5-009, consistent with submit()).
- Security/abuse: PASS — no new input surface beyond operator-fed queue payloads (already poison-handled upstream); no secrets/PII in new logs; specialist tool surface enum-closed.
- Performance/scalability: MINOR — R-W5-007 (unbounded suggestions/log flood).
- Migration/rollback: PASS — no schema/migration changes; every feature flag-off reversible; ADR-0044 rollback documented.
- Observability: PASS — tick outcome log, per-fact suggestion log, emit-failure error log, panel-failure warnings; cost gate intentionally ledger-silent per ADR-0040.
- Maintainability/conventions: PASS with R-W5-005/R-W5-008 — enum style `(str, enum.Enum)` ✓, import-conventions lock green (21 [RAN]), no new TYPE_CHECKING block (daemon import added inside existing block), zero-baseline honored (ruff 0, mypy 3 unchanged).
- Docs impact: PASS — ADR-0044 filed/indexed, ~120 cites semantically re-pointed (sampled ✓), lock 5 green [RAN]; persistence:178 best-holder flagged by W5-DOCS honestly.

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | curator.py cost-gate loop | per-fact log.info + unbounded list append | log flood at scale | low | low | DO LATER (cap suggestions at, say, 100 + count) |
| 2 | runner.py _attention_top | full-lineage reads per 2s tick × 50 lineages | O(events) per tick | med | med | NOT WORTH IT now (shares existing panel shape; revisit if panel latency matters) |
| 3 | specialist CODING_TOOL_NAMES literal | duplicates READ_ONLY+MUTATING lists (verified equal today) | drift risk | low | low | DO LATER (pin by test, R-W5-005) |

## 8. Cross-agent / cross-task interactions observed
- W5-IMPL-02 owned runtime.py (registration) while W5-IMPL-03's NEED targets runtime.py `_build_memory_creator` — disjoint regions, same file; the one-line NEED fix slots in without conflict.
- W5-IMPL-01's curator.py edit shifted `run_maintenance` docs cites; W5-DOCS re-pointed curator:141/:154 → :160 (verified in their table) — docs residual closed.
- W5-IMPL-03's creator.py edits shift later creator.py doc cites; W5-DOCS sweep table covers creator.py:1515-class cites as Correct at final HEAD — residual closed at HEAD, but any post-sweep src edit re-stales (standing gate item).
- Specialist block-emit fires for ALL guard kinds incl. pre-existing non-coding delegations — ADR-0041-mandated; rollback_count semantics now include routine fan-out/depth rejections. Replay/recovery suites green, so no observed breakage; operator dashboards counting rollbacks will see new volume (disclose in final report).

## 9. What I could not verify
- PostgreSQL substrate for cost-gate pagination (no PAA_TEST_POSTGRES_DSN/server on host) — R-W5-002 is reasoned from DDL param typing, not executed.
- Full test suite not run (scoped only, per standing orders).
- Daemon allocation tick under a real Runtime (tests use SimpleNamespace/FakeOrchestrator; the real chain daemon→orchestrator.run_allocation→allocate_attention is covered by W4's test_allocation_callsite_p007.py, 54 tests with creator suite passed [RAN], but not re-asserted here).
- Whether `paused` lineages etc. interact with ROLLED_BACK phase from the new emit — recovery sweep compatibility read at replay level only (ROLLED_BACK resumable, not terminal ✓).

## Commands run
- [RAN] `.venv\Scripts\python.exe -m ruff check src tests` -> All checks passed!
- [RAN] `.venv\Scripts\python.exe -m mypy src` -> 3 errors, all pre-existing sandbox/subprocess_backend.py:548 (baseline unchanged)
- [RAN] `.venv\Scripts\python.exe -m pytest tests/agents/test_coding_specialist_p011.py tests/context/test_allocator_scheduling_p007b.py tests/memory/test_cost_gate_p008.py tests/context/test_metrics.py -q` -> exit 0, no failures (72 dots × 3 lines; exact total not captured)
- [RAN] same 4 files + tests/tui + docs lock run: only failure = tests/tui/test_dashboard.py::test_ledger_with_events, ValueError at dashboard.py:347 `%H:%:%S` — pre-existing baseline category B
- [RAN] `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` -> 5 passed
- [RAN] `.venv\Scripts\python.exe -m pytest tests/conventions/test_import_conventions.py -q` -> 21 passed
- [RAN] `.venv\Scripts\python.exe -m pytest tests/agents/test_allocation_callsite_p007.py tests/memory/test_creator.py -q` -> 54 passed
- [RAN] direct import probes (tool coverage, READ_ONLY set equality) — results inline above
- [RAN] ADR-0044 citation spot-check vs live facts.py/dialect.py lines — all sampled cites hold
