# SUMMARY — 20261106-memory-remembering-agent

Branch: `ops/20261106-memory-remembering-agent` · Base: `eb6606e24d33f56b1dd7d2d811110b77940b8318` (main) · Head: `a05c08a` · Waves: 4 (W1–W4) + Final (REV → IMPR → DELTA) + Install (BLOCKED, retry-2 in flight) · Agents: ~30 (investigators, implementers, reviewers, improvers) · Dates: 2026-11-06 → 2026-11-07 · Plan: `C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md` (APPROVED, D1–D6)

## 1. Outcome in brief

The memory-remembering-agent plan is **implemented and gate-green at the wave level (C01–C07 all CLOSED)**. The agent can now encode a fact (cold-lake → creator + embedding sink), retain it across restarts (pending drain + curator schedule), and retrieve it by paraphrase through the planner's `_semantic` path (C01–C04), with extraction-quality counters and config hygiene (C05–C06) and e2e proof (C07). Two final whole-branch reviews (`APPROVE-WITH-CHANGES`) found 1 MAJOR (stale operator docs / missing R3 snapshot guidance) and 3 MINORs (stale coder note, fallback floor divergence, e2e touch bypass); the MAJOR and all in-write-set MINORs were closed by **FINAL-IMPR-02**, and the delta review (**APPROVE**, 0 blocker/major, 1 NIT) confirms the fixes. **Install/launch (§K) is NOT done** — `FINAL-INSTALL-01` blocked twice on dirty-tree stop conditions and retry-2 is still in flight at summary time; R1/R2/R3 remain operator post-launch tasks.

## 2. Plan coverage

| Plan item | Task | Status | Commits (code) | Report |
|---|---|---|---|---|
| C01 | Wire `VectorEmbeddingSink` in runtime (degraded `None`+warning) | DONE | `c5766c8` (W1) | w1-impl + ledger |
| C01b (synthesis) | Align sink payload keys (`object_value`/`confidence`/`importance`) with `_semantic` reader | DONE | `c5766c8` (W1) | 01-work-ledger.md synthesis |
| C02 | `drain_pending_embeddings` + daemon/API/CLI callers | DONE | `c5766c8` + `e966b68` (W1) | w1-impl + w1-improve |
| C03 | Touch on recall (5 paths, delivered-only) | DONE | `3506e30` + `2c72f96` (W2) | w2-impl + w2-improve |
| C04 | Schedule `MemoryCurator.run_maintenance` in daemon (6h, first-tick preview) | DONE | `70dd91f` (W2) + `d7801a8` (W4 preview-consume) | w2-impl + w4-improve |
| C05 | Extractor drop counters + I/we→`user` @0.3 | DONE | `648f979` + `7da607f` (W3) | w3-impl + w3-improve |
| C06 | `prune_confidence_floor` explicit-only wiring + `decay_lambda` deprecation | DONE | `f4d0ca8` (W3) | w3-impl |
| C07 | E2E + observability (pending-count, curator logs, INFO) | DONE | `326b16d` + `d7801a8` (W4) | w4-impl (W4-IMPL-08 recovered from BLOCKED W4-IMPL-07) |
| T01–T08 | Tests (sink/drain/touch/curator/extractor/floor/e2e) | DONE | per-wave | w1–w4-impl/review/improve |
| FINAL (F-01 + FINAL-01..05, F-02..05) | Operator runbook, coder note, redaction, floor forwarding, handle-touch e2e | DONE (4 FIXED, 1 PARTIAL, 1 REJECTED, 4 DEFERRED) | `1889808` (code) + `91da8c8` (docs) | final/IMPR-01.md |
| §K Install/launch | `uv` install + gate + daemon/API smoke | **NOT DONE** (BLOCKED → retry-2 in flight) | — | final/INSTALL-01.md (BLOCKED) |

### Re-scopes (operator decisions, recorded in `01-work-ledger.md`)

- **T08 / Success criterion 1**: paraphrase proof is planner `_semantic` + direct vector search; coder `recall_facts` stays **keyword-only**, documented. Literal plan criterion 1 (via `recall_facts`/`GET /memory/search`) is therefore **not met as written**; the re-scoped proof is.
- **C03 touch is delivered-only** (post-`handle()`, graph `graph:` ids excluded), not literal "every recall".
- **C06 floor is explicit-only**: global `prune_confidence_floor` applies only when explicitly set (`model_fields_set`), else per-domain floors (INV-02) — no blanket override.
- **C04 preview**: first tick is `dry_run` preview, consumed only on `completed_fully and errors==0`; permanently-broken curator degrades to preview-forever with loud warning (safe direction), not delete-after-N.

## 3. Changes by domain

**Runtime** (`src/paa/runtime.py:433-493`): `_build_memory_creator` threads `_try_vector` result + embedder factory into `VectorEmbeddingSink(vector_store, embedder)`; degraded `None`+warning on any failure. `memory.embedding_sink` wired/degraded logged.

**Memory creator** (`src/paa/memory/creator.py`): `VectorEmbeddingSink.enqueue` embeds `fact.statement` with reader keys (`object_value`/`confidence`/`importance`); `drain_pending_embeddings(limit=100)` (one batch/call, never flips to terminal `failed`, exact `pending_count` COUNT, `memory.embedding_drain` INFO); `ExtractionDropCounts` (8 fields, snapshot before await) + `drop_counts` on `ProcessingResult`/`BatchReport`; first-person `I/we→user @0.3`; `redact()` on drain error.

**Recall + touch**: planner `_hot_facts` (LIKE, limit 60) + `_semantic` (vector, limit 30) + graph, worker hot-only, coder `recall_facts` keyword-only; touch = `DecaySweeper.touch_many` (`last_queried_at=now, use_count+1`) delivered-only on 5 surfaces (context×2, coder, API×2, CLI×2). Retention effect flows via `last_queried_at → effective_confidence`, not `importance_index`.

**Daemon/curator** (`src/paa/daemon.py:200-331`): ingestion loop drains after each `run_batch(limit=25)`; maintenance 6h tick calls `MemoryCurator.run_maintenance(budget, dry_run=preview)` with fallback `DecaySweeper.sweep(dry_run=…)` on construction failure + explicit floor forwarding (`explicit_prune_floor()`); `daemon.curation_completed` INFO with `report.summary()`. Curator phases: decay → prune_edges → consolidate → orphans → reconcile → analyze (budget checked between phases).

**Config** (`src/paa/config.py`): `prune_confidence_floor=0.15` explicit-set-only; `decay_lambda` deprecated (kept for compat, warn-on-use, sole source `domain_policy`).

**Tests**: `tests/memory/test_e2e_remember_recall.py` (19 tests incl. `test_paraphrase_recall_via_handle_advances_use_count` — real `PlannerContextBuilder.handle()` advances `use_count` 0→1), `tests/daemon/test_maintenance_loop.py` (fallback preview + floor spy), `test_creator/test_decay/test_curator/test_llm_extractor/test_memory_creator`, API/CLI touch tests.

**Operator docs** (`docs/modules/daemon.md`, `docs/modules/memory.md`, `docs/operations/deployment-and-runtime.md`, `docs/deployment/docker.md` + 17 citation re-points): authoritative runbook (*Memory curation* — full curator not bare sweep, snapshot-before-delete, **cold-lake replay restores, code revert does not**), backlog-watch note, Docker hash-embedder caveat. All `path:line` cites verified/read, conventions green.

## 4. Deep-Dive Spotlight

None — all tasks were SURFACE-class (single domain, no contract/schema change). Two deep investigations were required (W4-IMPL-07 blocked after 32 min investigation-only → superseded by time-boxed W4-IMPL-08; FINAL-IMPR-01 doom loop in polling commands → recovered by FINAL-IMPR-02); both ended as DONE after audit of uncommitted diffs.

## 5. Verification evidence

| Step | Command | Result | vs baseline (00-mission.md) |
|---|---|---|---|
| Lint | `ruff check src tests` | All checks passed! | 0 → 0 |
| Typecheck | `mypy src` | 3 errors `sandbox/subprocess_backend.py:548` (win32) | identical (pre-existing) |
| Scoped (final) | `pytest tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py tests/conventions/test_docs_references.py -q` | 37 passed | — |
| Scoped (waves) | `gate-w1-2, gate-w2-2, gate-w3-2, gate-w4-2` | green each | none new |
| Reversal | FINAL-IMPR-02 executed reversal for FINAL-02 (floor-forwarding tests fail without fix) | pass | — |
| Full suite | `pytest tests/ -q` | **NOT run to completion in final gate** (baseline 12 pre-existing Windows failures; final gate scoped until install) | — |

## 6. Review outcomes

**FINAL-REV-01 (correctness, whole branch)**: `APPROVE-WITH-CHANGES` — 0 BLOCKER, 0 MAJOR, 3 MINOR (FINAL-01..03), 2 NIT. All 14 claims verified, no band-aids.

**FINAL-REV-02 (operability, whole branch)**: `APPROVE-WITH-CHANGES` — 0 BLOCKER, 1 MAJOR (F-01 operator docs / R3 snapshot missing), 3 MINOR, 1 NIT. No schema change verified; 3 unredacted error lines flagged.

**FINAL-IMPR-02 (improvement)**: FIXED 4 (F-01 runbook, FINAL-01 coder note, FINAL-02 floor forwarding, FINAL-03 handle-touch e2e, F-02 in-set redaction), PARTIAL 1 (F-02 remainder: `runtime.py:481`, `cli/main.py:649` outside write-set → NEED posted), REJECTED 1 (F-05 plan edit — plan is outside repo, correction carried in operator docs), DEFERRED 4 (F-03 single-flight, F-04 throttle, FINAL-04 helper dedup, FINAL-05 plumbing). Scoped gate 32/32, ruff 0, mypy 3 baseline.

**FINAL-DELTA-REV**: `APPROVE` — 0 blocker/major, 1 NIT (`29d60e4` added 2 RFC docs at `docs/` root, non-code noise). All 6 claims checks verified.

Per-wave reviews: every wave closed with delta APPROVE (W4 delta APPROVE after 1 MAJOR R-W4-001 fixed). No BLOCKER/MAJOR survived.

## 7. Deferred / open items (ticket-ready)

| Item | Severity | Reason | Disposition |
|---|---|---|---|
| §K install/launch smoke | — | FINAL-INSTALL-01 BLOCKED twice (dirty tree); retry-2 in flight | **Re-run at `a05c08a`**; gate + isolated smoke evidence pending |
| R1 hash-embedder precision | Risk | lexical-only hash vectors mislead ranking; Docker lacks `embeddings` extra | Operator: 10 paraphrase spot-checks on real embedder before trusting ranking |
| R2 touch-immortality | Risk | frequently-recalled facts may starve eviction long-term | Operator: tune floors if eviction starves |
| R3 snapshot before first deleting tick | Risk | curator delete phases never ran in prod before C04 | Operator: snapshot `PAA_HOME` (runbook), enable phases one at a time |
| No replay job | Known gap | restore = manual (snapshot, reset signal, drain) | Documented non-goal; follow-up research |
| F-02 redaction remainder (`runtime.py:481`, `cli/main.py:649`) | MINOR | outside FINAL-IMPR-02 write-set | NEED posted; owner of those files to redact |
| F-03 single-flight drain + curator pagination | MINOR | concurrent drains duplicate embed work (idempotent); `fetch_all` safe at current scale | DEFERRED (DO-LATER) |
| F-04 drain throttle | MINOR | no rate dial; drain-at-embedder-speed | DEFERRED + operator backlog-watch note |
| FINAL-04 duplicate `_touch_recalled` (4 copies) | NIT | identical behavior today | DEFERRED refactor (DO-LATER) |
| FINAL-05 drain limit / reconcile preview 0 | NIT | batch-100 reasonable; preview 0 truthful for stores lacking retain-only | DEFERRED (documented) |
| `docs/` root RFC pair (`A Disciplined System…`, `Core Architecture…`, added `29d60e4`) | — | substantive, not junk; tracking/move decision pending | Operator decision (DOCS-AUDIT-01, 0 delete-candidates) |

## 8. Out-of-plan discoveries

**`/docs` structure comparison** (investigators A/B/C, read-only, at `a05c08a`). The two RFC docs added at `docs/` root are aspirational, not build specs (no ADR adopts them); adopting them verbatim would reintroduce ZeroDivision, silent winner-picking, and ledger-invariant violations.

- **Doc A — A Disciplined System for Computational Attention Allocation**: achieved 4 / partial 8 / not-integrated 6 of 18 proposals. Achieved: ETL, quarantine, decay/prune, 6-phase curator, deterministic ranking, sink+drain. Not integrated: Attention Allocator (`allocate_attention()`), memory-budget cost, T0–T4 tiers/TTLs, governance `identity/strategy/constitution/principles.md`, 5 dashboards, Phase 1–4 roadmap metrics. 7 doc-vs-code contradictions (doc "keep higher" vs live never-auto-resolve; doc formula `(I·S·F·U)/(E·P·H)` ZeroDivision vs live `(1+P)(1+R)`; doc cold-is-truth vs live ledger-is-truth).
- **Doc B — Core Architecture RFC (v4.0+v4.1 in one file)**: achieved 10 / partial 6 / not-integrated 2 of 18. Achieved: ledger/replay, polyglot substrates, 9-agent hierarchy, self-improvement, 18 domains, bounded context, USA/MCP, permissions, recursion guards, world model, deterministic validation. Not integrated: **21 specialists** (`agents/specialists/__init__.py` 0 lines), **markdown vault 11/13 files**, v4.1's 9 extra Kuzu edges. RFC-internal contradictions documented (incomplete ENUM, wrong terminal set, Postgres-only vs parity lock, `pg_trgm` vs LIKE-helper, MinIO vs S3-protocol, "exactly 2h nightly" vs live 6h+budget).
- **Current code (C)**: 11 hard limits — hash embedder lexical-only, Docker lacks `embeddings`, coder recall keyword-only, floors manual-only, no replay job, no drain throttle, in-memory preview/resume (restart re-previews), frozen vector payload (confidence snapshot, not decayed value), non-ASCII parity bound, narrow extraction, 4 dup touch helpers, 2 unredacted error lines.

Full tables with `file:line` in investigator A/B/C reports (in chat; not filed in run folder).

## 9. Temporary mitigations (user-approved)

None.

## 10. Risks, rollout and operational notes

- **No schema/persistence changes** — no migration, parity lock unaffected (`git diff` on `storage/`+`ledger/` empty vs base).
- **Backward compat**: touch mutates `use_count`/`last_queried_at` on reads (semantically new writes, same columns); sink adds index rows (additive); `decay_lambda` deprecated-not-removed (old `.env` keeps parsing). Floors: defaults byte-identical when unset (explicit-only).
- **Rollout**: C01→C07 sequentially (verified per wave). **Rollback**: per-change revert, no migration, no point of no return (derived data only). **Curator deletes are not restorable by code revert** — restore is cold-lake replay per runbook (`docs/operations/deployment-and-runtime.md` *Memory curation*).
- **Security**: deterministic ranking unchanged (no LLM rerank); drain error redacted in-set, 2 lines remain (deferred); no model input to ranking changed.
- **First-run**: curator first tick previews (6h after boot — `sleep` first), second deletes; snapshot `PAA_HOME` before enabling. Production curator delete volumes are unverified on real data.

## 11. Assumptions and decisions

- D1–D6 (operator, 2026-11-06): scope core+extraction quality; sink via factory (degraded `None`); touch all recall (delivered-only refined); curator in daemon; deterministic ranking; plan location. All confirmed.
- A-002 confirmed (no deploy cron → no double-run risk). C01b payload alignment discovered post-synthesis. C03 rationale corrected to `effective_confidence`, not `importance_index`. T08 re-scoped (planner `_semantic`). Baseline mypy 3 / pytest 12 pre-existing (Windows host), untouched.
- Approval: plan APPROVED 2026-11-06; wave permissions lifted ("don't ask until all waves done"); §K install requires explicit word — given, agent spawned, currently blocked/retrying.

## 12. How to review this branch

Read in order: W1 code `c5766c8` → W1 improve `e966b68` → W2 `3506e30`,`70dd91f` → W3 `648f979`,`f4d0ca8`,`7da607f` → W4 `326b16d`,`d7801a8` → FINAL `1889808`,`91da8c8` → delta `617c623`. Run folder: `docs/agent-ops/20261106-memory-remembering-agent/` (`01-work-ledger.md` disposition per wave; `gates/gate-w*-2.md` green; `reports/final/` verdicts; `COMMS.md` trail).

## 13. Agent roster (excerpt)

| Agent | Role | Task | Report |
|---|---|---|---|
| Wave-0 ×3 | Investigators | creator/runtime, decay/curator/config, recall/extractor | phase-0 digests |
| W1/W2/W3/W4 implementers (+W4-IMPL-08 recovery) | Implementers | C01–C07 | w1–w4-impl |
| W1–W4 reviewers + TREV | Reviewers | per-wave audit | w1–w4-review |
| W1–W4 improvers (+DOCS) | Improvers | MAJOR/MINOR fixes | w1–w4-improve |
| FINAL-REV-01 / 02 | Final reviewers | whole-branch correctness / operability | final/FINAL-01*, FINAL-02* |
| FINAL-IMPR-01 / 02 | Final improvers | F-01 + FINAL-01..05 (01 looped → 02 recovered) | final/IMPR-01.md |
| FINAL-DELTA-REV | Delta reviewer | FINAL-IMPR-02 audit | final/DELTA-review.md |
| FINAL-INSTALL-01 | Installer | §K install/launch | final/INSTALL-01.md (BLOCKED) |
| Investigators A/B/C | Read-only | Doc A / Doc B / current code | chat (not filed) |
| DOCS-AUDIT-01 | Auditor | `/docs` classification (282, 0 junk) | final/DOCS-AUDIT-01.md |

## 14. Residual risk and unverified claims

- **Unverified**: §K install/launch — no install or smoke evidence at summary time (retry-2 in flight). Full `pytest tests/` final run not executed to completion (900s+ suite; scoped gates green).
- **Unverified**: live Qdrant/real-embedder ranking fidelity; curator eviction volumes on real data (operator post-launch).
- **Residual risk**: Docker hash-embedder (R1), touch-immortality (R2), first-delete safety (R3) — mitigations documented, not operator action. F-02 2-line redaction remainder. Doc A/B integration would need ADR + plan (currently aspirational).
- **Branch is merge-ready** for the C01–C07 scope (all waves + final delta APPROVE, scoped gate green) — **not yet for §K install/launch**.
