<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-03 | domain: P-002 query-time confidence derivation (vector path) | status: COMPLETE
items: P-002 Fix A done (vector path now derives; hot path untouched)
proposed_tasks: 0 (implementation)
top_risks: (1) Postgres IN placeholder path not run live (assumed via codebase ? translation); (2) semantic-tier confidence_floor nuance documented (floor applies to hot pool; semantic ranks by relevance*confidence)
cross_domain_conflicts: none (sole owner context_agents.py this wave; P-004 depends on this landing first)
out_of_plan_discoveries: 0
confidence: high (9 new tests green, 116 regression green, mypy 3, ruff 0, fail-before 7/9 verified)
last_comms_line_read: 2026-11-07T08:10:11Z W1-IMPL-01 -> ORCH | DONE | T-014-P014
<!-- ORCH-DIGEST:END -->

# Implementation report — W1-IMPL-03 — P-002 (T-002-P002)

## Status: COMPLETE

## 0. Scope and method
Task: implement plan P-002 Fix A exactly per brief `docs/agent-ops/20261107-integration-fix/briefs/W1-IMPL-03.md`.
WRITE-SET only: `src/paa/agents/context_agents.py`, `src/paa/memory/creator.py`, `tests/agents/test_semantic_derivation_p002.py` (new). READ-ONLY: everything else including `gatherer.py`, `facts.py`, `decay.py`.
Required reading done in brief order: STANDING-ORDERS, AGENTS.md, 00-mission.md, plan rev5 `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` (§E P02 row, §H T02, D13), W0-INV-01 §P-002 (+§P-009 note), W0-INV-04 §P-004 tail, report templates, code entry points (decay.py:54-65, domains.py, facts.py:332-336 + 1183-1224, context_agents.py:53/194-262, creator.py:1073-1091, gatherer.py:103-152 read-only, tests/agents/test_context_agents.py).
Branch verified: `ops/20261107-integration-fix` [RAN git branch --show-current]. Working-tree at start dirty from siblings only (`config.py`, `COMMS.md`, sibling reports) — own files clean.
COMMS read at start; last line: 2026-11-07T13:33:39Z W1-IMPL-02 -> ALL | START | T-001-P001.

## Context Report (pre-implementation)
Root cause: paraphrase recall (`_semantic`) ranks by confidence snapshot frozen at ingest via `VectorEmbeddingSink.enqueue` payload literal (`creator.py:1073-1091` `fact.confidence` at enqueue time). Every touch advances `last_queried_at` (decay.py touch) making frozen payload staler; gatherer is pure and cannot re-derive without inputs it does not carry (`ContextElement` has confidence/domain but no last_queried_at/initial). Hot path proves correct pattern (`_hot_facts` derives via `effective_confidence`).
Affected files: `context_agents.py::_semantic` (hydrate + derive), `creator.py` (comment-only provenance), new test file. `gatherer.py` ZERO changes (pure stays pure). `facts.py` ZERO changes; batch query lives near `_semantic` using existing `self._db` (no FactRepository dependency, no constructor change).
Invariants preserved: ledger-is-truth (no payload writes, no sweep-refresh); `ContextElement` shape unchanged; `_semantic` signature additive only (`now` kwarg, default None -> wall clock like `effective_confidence`/`_to_fact`); payload keys unchanged (no migration, no re-index); `effective_confidence` signature unchanged; vector-failure `[]` path unchanged; hydration failure degrades to payload snapshot (semantic tier never emptied); unknown domain passthrough + `memory.fact_unknown_domain` warning.
Technical risks: N+1 if per-hit fetch (must be ONE batched `WHERE id IN`); placeholder dialect (`?` translated for Postgres — use `?` as codebase does); LIKE via like.py not needed; S608 per-line noqa only if ruff demands (follow `memory/facts.py` convention, never per-file widening); LIMIT tie-break not relevant (hydration is full IN set); `payload.get("confidence")` must remain exactly 1 hit in `src/paa/` (fallback only, derivation line must exist); importance/memory_domain: hydrated DB wins for hydrated hits (payload defaults 0.8/0.5 must not resurrect as ranking truth after hydration for hydrated fields); gatherer semantic tier ranks by `relevance*confidence` and does not confidence-filter (confidence_floor applies to hot tier) — T02 floor assertion demonstrated via hot-pool path + ranking demotion, documented honestly.
Expected behaviour: fact C0=1.0 enqueued, aged past λ half-life (semantic λ=0.002, ~300 days -> ~0.55), `_semantic(now=...)` returns `approx(effective_confidence(1.0, last_queried, "semantic", now))`; payload `confidence` still 1.0 (provenance); missing-key hit hydrates (no 0.8 leak); DB explode -> payload fallback + warning + non-empty tier; 100x load -> 1 hydration query per search.

Phases (atomic):
- Phase A: `context_agents.py::_semantic` hydrate-in-_semantic (batched, derived, degrade, now injection, trace tag).
- Phase B: `creator.py` comment-only provenance.
- Phase C: `tests/agents/test_semantic_derivation_p002.py` (T02 + missing-key + degrade + unknown-domain + idempotency + batch-count + load-100x).
- Phase D: scoped verification (new tests + test_decay + test_gatherer + ruff/mypy scoped, fail-before check via stash-free logic inspection + direct run before/after? Must prove tests fail before).
- Phase E: self-review + final review, commit code, commit report separately.

## Phase log
### Phase A — `context_agents.py::_semantic` hydrate-in-_semantic (DONE, self-reviewed)
- Signature additive only: `now: datetime | None = None` (default None -> wall clock via `effective_confidence`/`idle_days`, same as `decay.py`/`_to_fact`). No caller change needed; production `handle()` calls without `now` derive against wall clock, tests inject deterministically.
- After `vector_store.search` (vector-failure `[]` path unchanged), ONE batched hydration: `SELECT id, initial_confidence, last_queried_at, memory_domain, importance FROM hot_serving_active_facts WHERE id IN (?,...)` via existing `self._db.fetch_all` (RelationalBackend at runtime, `?` translated for Postgres; placeholders from deduped hit ids; bounded by ≤30-hit limit; no FactRepository constructor need — `_db` already present, so no NEED posted). No chunking (30 < SQLite 999-var limit; overflow degrades to fallback, tier still serves).
- Derivation per hydrated hit: `effective_confidence(initial_confidence, from_iso(last_queried_at), memory_domain, now=now)` with live `last_queried_at`; `importance`/`memory_domain` from DB win (payload 0.8/0.5 defaults never resurrect for hydrated hits). Unknown domain: `except KeyError` -> stored passthrough + `memory.fact_unknown_domain` warning (mirrors `_to_fact`/`_hot_facts` exactly; ValueError left to propagate as in hot path — DB CHECK prevents it).
- Fallback per non-hydrated hit (db None, hydration exception, or missing live row/deleted fact): payload snapshot `confidence=float(hit.payload.get("confidence", 0.8))`, `importance=float(hit.payload.get("importance", 0.5))`, domain default `"semantic"`. Hydration exception logs `context.vector_search_failed` (same event as vector failure) and `hydrated={}` — tier still serves, one element per hit, hit order preserved.
- Trace tag `PLAN:integration-fix:P002 (D13...)` at hydration site with DECISION note. No caching/memo of derived value (per-query only). `gatherer.py` ZERO changes, `facts.py`/`decay.py` ZERO changes.
- Grep verification: [RAN grep payload.get("confidence") -> 1 hit context_agents.py:309 fallback only]; [RAN grep PLAN:integration-fix:P002 -> hydration site].

### Phase B — `creator.py` comment-only provenance (DONE, self-reviewed)
- Comment-only above payload literal (no field add, no widening, Fix B rejected note): documents `confidence` as enqueue-time provenance snapshot, never ranking truth, with cause (every touch advances clock, frozen timestamp stale on first recall). Verified via diff: only comment lines added.

### Phase C — `tests/agents/test_semantic_derivation_p002.py` (DONE, self-reviewed)
9 tests (T02 main uses real NumpyVectorStore + VectorEmbeddingSink + _SynonymEmbedder 384-d; rest use fake-vector pattern from test_context_agents.py):
- `test_t02_derived_confidence_and_provenance_and_floor`: C0=1.0 enqueued via sink (payload 1.0 asserted), aged 300d (semantic λ=0.002 -> ~0.549), `_semantic(now=...)` == `pytest.approx(effective_confidence(1.0, old, "semantic", now), abs=1e-6)`, <0.70 floor, payload still 1.0 after aging, gatherer `_candidate_pool` hot-path drops derived but keeps payload-trusting 1.0 element.
- `test_missing_confidence_key_hydrated_value_wins`: payload missing confidence/importance/domain (tool λ=0.05, 30d -> ~0.223, importance 0.77) -> hydrated wins, no 0.8/0.5/semantic leak.
- `test_hydration_db_failure_degrades_to_payload_snapshot`: `fetch_all` explodes -> payload 0.9 fallback + `context.vector_search_failed` in captured logs + tier non-empty.
- `test_unknown_domain_passthrough_not_crash`: domain `not_a_domain`, C0=0.9 vs payload 1.0 -> 0.9 passthrough + `memory.fact_unknown_domain` warning.
- `test_idempotent_same_now_same_derived`: same `now` twice -> identical derived.
- `test_batch_hydrate_one_query_per_search`: 30 hits -> exactly 1 `fetch_all` with `WHERE id IN`.
- `test_load_100x_hydration_query_count`: 100 searches x 5 hits -> exactly 100 queries (never 500).
- `test_ranking_uses_derived_not_payload`: same relevance, same payload 1.0, fresh outranks stale via `_rank_key` on derived.
- `test_vector_failure_still_returns_empty`: exploding vector store -> `[]` + warning (existing path unchanged).

### Phase D — Scoped verification (DONE)
- [RAN .venv\Scripts\python.exe -m pytest tests/agents/test_semantic_derivation_p002.py -v -> 9 passed in 2.15s] (after fix)
- Fail-before proof (file-copy revert, no git stash/reset): reverted `_semantic` to payload-only via edit, [RAN pytest ... -> 7 failed, 2 passed in 1.54s] (T02 got 1.0 vs expected 0.549; missing-key got 0.8 vs 0.223; degrade missing warning; unknown got payload 1.0 vs initial 0.9; batch 0 vs 1 query; load 0 vs 100; ranking 1.0 == 1.0). Restored fixed file via copy; [RAN pytest ... -q -> 9 passed].
- [RAN pytest tests/memory/test_decay.py tests/context/test_gatherer.py -v -> 116 passed (decay 54 collected incl. parametrize, 51 defs; gatherer 62)] unchanged.
- [RAN pytest tests/agents/test_context_agents.py -q -> 23 passed] (existing `_semantic` touch tests incl. `test_planner_semantic_recall_touches_the_hit_fact`, `test_low_relevance_semantic_hit_is_not_touched` still green).
- [RAN pytest tests/memory/test_e2e_remember_recall.py::test_e2e_paraphrase_retrieved_after_restart -q -> 1 passed] (paraphrase recall unchanged).
- [RAN ruff check src/paa/agents/context_agents.py src/paa/memory/creator.py tests/agents/test_semantic_derivation_p002.py -> All checks passed]
- [RAN mypy src/paa/agents/context_agents.py src/paa/memory/creator.py -> Success: no issues] ; [RAN mypy src -> 3 errors in 1 file (subprocess_backend.py:548, baseline, unchanged)]
- Acceptance greps: `payload.get("confidence")` = 1 hit (fallback only); `effective_confidence` derivation + `PLAN:integration-fix:P002` present in `_semantic`.

### Phase E — Self-review + final review (DONE, see Review Audit)
- Re-read full diff from scratch (shell git diff). Findings: one MINOR typo `Batch-hydre` fixed by hand; no CRITICAL/MAJOR. Mutation check: revert core -> 7/9 new tests fail for the right reasons (see Phase D). Load/hostile/operability/reversal answered below.

## Review Audit
- Phase A self-review: CRITICAL none, MAJOR none, MINOR 1 typo (Batch-hydre -> Batch-hydrate) FIXED. Edge checks: empty hits early-return (no query), duplicate ids deduped, missing row per-hit fallback, db None skips hydration silently (backward compatible for db-less builders), unbounded limit overflow degrades (not crash), ValueError from corrupt row deliberately not swallowed (mirrors `_hot_facts`/`_to_fact` CHECK-prevented path). No N+1 (single IN query; batch/load tests pin count). No caching.
- Phase C self-review: tests are behaviour-focused, deterministic (injected now), isolated (function-scoped db/tmp_path, store closed in finally). Hostile unknown-domain + missing-key + degrade covered; boundaries (floor 0.70, 0.8/0.5 defaults) pinned; concurrency not applicable (read-only derivation, no shared mutation); idempotency pinned. Mutation: if derivation reverted to payload, T02/missing-key/batch/ranking fail; if hydration reverted to per-hit, batch/load fail; if degrade reverted to empty tier, degrade test fails. No test-weakening, no mocks of unit under test (fakes are vector targets, not `_semantic` itself).
- Final review: cross-phase — `handle()` callers unaffected (additive kwarg), E2E paraphrase still green, context_agents 23 green. No leftover debug, no unrelated file edits (git diff shows only WRITE-SET + test file). Backward compat: payload keys unchanged, `ContextElement` unchanged, `Fact` unchanged, vector `[]` path unchanged, db-less builders still serve payload fallback. Independent review: not required by brief for SURFACE (single-file reader change, no schema/ledger); status COMPLETE (not REVIEW_REQUIRED). If orchestrator wants fresh-context reviewer, diff is the Phase A+B+C set above.

## Long-run test answers (per standing orders)
- **Source:** ranking equals direct `effective_confidence` (T02 approx abs=1e-6) and gatherer `_rank_key` on derived (ranking test); payload provenance still 1.0.
- **Load (100x):** 30-hit batch = 1 query; 100x searches over 5 hits = 100 queries (one per search, never per-hit). No unbounded collection (hits bounded by limit param; ids deduped; elements list is hits-sized).
- **Hostile:** unknown `memory_domain` in DB -> passthrough + warning, no crash; missing payload keys -> hydrated wins (no 0.8/0.5 leak); NUL/oversized payloads not applicable (IN ids are UUIDs, bound as params, not interpolated); injection via id impossible (parameter binding).
- **Operability:** vector down -> `[]` + warning (unchanged); DB down during hydration -> payload fallback + `context.vector_search_failed` warning, tier non-empty (new); observe via `memory.fact_unknown_domain` / `context.vector_search_failed` events; rollback = revert `_semantic` (payload-only path returns; no migration, no payload format change, no fleet re-index); migrate: none.
- **Reversal:** reverting hydration block makes `test_t02...`, `test_missing...`, `test_batch...`, `test_load...`, `test_ranking...` fail (verified 7 failed before fix). Reverting degrade to empty tier fails `test_hydration...`. Reverting unknown handling to crash fails `test_unknown...`.

## Unverified Assumptions & Remaining Risks
- [ASSUMED] Production `handle()` path (planner builder with real Qdrant) derives identically to tested Numpy/fake paths — verified by inspection (same `fetch_all`/`effective_confidence` code) but not run against live Qdrant/Postgres (scoped env, no containers per brief).
- [ASSUMED] Postgres placeholder translation (`?` -> `%s` for `WHERE id IN (?,...)`) works because codebase uses `?` everywhere and `signals.py`/`sqlite_backend.py` use the same IN pattern — not run live against Postgres (would need DSN).
- Semantic-tier confidence floor nuance: gatherer `_candidate_pool` applies `confidence_floor` to hot tier only; semantic tier ranks by `relevance*confidence` (no floor filter). T02 floor assertion therefore demonstrates the hot-pool drop on the derived value (the stale fact below floor is dropped even though payload says 1.0) plus ranking demotion on the semantic path. This matches code reality; if a future plan wants a hard semantic confidence cutoff, that is a gatherer change (out of scope, pure stays pure).
- `limit` > SQLite variable limit (~999) would overflow the IN query and degrade to payload fallback (tier still serves) — acceptable within the ≤30-hit contract; chunking deliberately not added to keep ONE-query guarantee.

## Git State
- Branch: `ops/20261107-integration-fix` [RAN git branch --show-current -> `ops/20261107-integration-fix`]
- Code commit: `298b2f2cc7e49d6f370b477f5fa16f9bb2d50d16` (3 files, explicit paths, trailers Task/Agent/Wave/Report)
- Report commit: separate docs-only commit listing code SHA above
- Working tree after code commit: siblings' `runtime.py`/`cli/main.py`/`COMMS.md` remain uncommitted (never touched by this agent); this agent's files clean after code commit except untracked report.
- Last COMMS line read: `2026-11-07T08:10:11Z W1-IMPL-01 -> ORCH | DONE | T-014-P014 | FACT P-014 done...`
- Files changed by this agent: `src/paa/agents/context_agents.py`, `src/paa/memory/creator.py`, `tests/agents/test_semantic_derivation_p002.py` (new), `docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-03.md`.

