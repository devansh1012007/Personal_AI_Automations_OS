<!-- ORCH-DIGEST:START -->
agent: FINAL-REV-02 | scope: whole branch operability (eb6606e..HEAD) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 1 | MINOR 3 | NIT 1
claims_audit: 9 claims checked / 3 unsupported or false
band_aids_found: none
top_3_risks: stale operator docs hide first curator deletes (R3 snapshot absent); Docker hash-embedder R1 caveat not in deploy doc; unredacted new error lines
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only; per-change revert traced, no code reverted)
confidence: med (scoped tests ran; 100x/concurrency/large-scale not load-tested, reasoned from code)
<!-- ORCH-DIGEST:END -->

# Final operability review — FINAL-REV-02

## 0. Method
Whole-branch diff `eb6606e24d33f56b1dd7d2d811110b77940b8318..HEAD` (10 src files, 0 storage/ledger files) read in context: `daemon.py`, `memory/{creator,curator,decay}.py`, `runtime.py`, `config.py`, `api/app.py`, `cli/main.py`, `agents/context_agents.py`, `coder/runtime_tools.py`, plus `Dockerfile`, `pyproject.toml`, operator docs. No source edited. Scoped runs only [RAN].
Last COMMS line read: 59 (`[2026-11-06T21:14:26Z] ORCH -> ALL | DECISION | WAVE-END`).

## 1. Blind-pass findings (before implementer reports)
See §4. Operability lens only; correctness not duplicated except where it creates a data-loss/secret hole.

## 2. Claims audit
| Claim (plan/ledger/docs) | Verified? | Evidence |
|---|---|---|
| No schema change; parity lock unaffected | YES [VERIFIED] | `git diff --name-only base..HEAD -- src/` = 10 files, none under `storage/` or `ledger/`; `schema_sqlite.sql`/`schema_postgres.sql`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml` diff empty [RAN git diff] |
| Rollback = revert commit, "point of no return: none" (plan §K) | FALSE for curator deletes | Reverting code does not restore facts deleted by prune/consolidate/orphans; restore path is cold-lake replay, undocumented. See F-01 |
| R3: snapshot cold lake before C04; enable phases one at a time | PARTLY | Code first-tick dry-run preview exists [VERIFIED daemon.py:230-244,263-320] but snapshot/enable-gradually guidance appears nowhere in operator docs [VERIFIED grep preview/snapshot in docs/modules/daemon.md, modules/memory.md, operations/deployment-and-runtime.md, deployment/docker.md = 0 hits] |
| Docker ships without embeddings extra, documented (plan pre-mortem ⑤, C01) | PARTLY | Caveat only in `Dockerfile:27-30` header + `pyproject.toml:40-42`; `docs/deployment/docker.md` has 0 hash/embedder hits [VERIFIED grep] — operator following deploy doc is not told prod semantic recall is hash-based (R1) |
| Drain pending count is bounded / not a per-tick DoS | YES (with nuance) | Idle branch skips COUNT [VERIFIED creator.py:1521-1533]; success/failure paths do 1 COUNT per call over partial index `idx_facts_pending_embedding` (`schema_postgres.sql:249`); daemon idle sleeps 1s so worst ~1 COUNT/s when sink absent [VERIFIED daemon.py:150-168]. Not a DoS; see F-04 for throughput |
| Touch failures never fail recall | YES | All 5 `_touch_recalled` copies catch, log, return [VERIFIED context_agents.py:421, api/app.py:362, cli/main.py:719, runtime_tools.py:904] |
| Curator budget is a hard 7200s cap | FALSE (honestly documented in code) | Budget checked between phases only; worst = budget + longest phase [VERIFIED curator.py:157-160, daemon.py:212-214]. Code says so; operator docs do not |
| Maintenance loop still described accurately | FALSE | `docs/modules/daemon.md:29,60` and `docs/operations/deployment-and-runtime.md:57` still say maintenance = `DecaySweeper(db).sweep()`; code now runs full curator [VERIFIED daemon.py:170-182,200-321] |
| Secret/PII redaction on new paths | PARTLY | Recall-touch/drain daemon+API lines use `redact()`; 3 new lines do not. See F-02 |

## 3. Long-run test
- **Source:** ledger invariant intact — no new `EventType`, no direct hot-table writes outside derived retention (`touch_many`) and creator/curator existing paths; cold-lake write rule preserved [VERIFIED diff]. `use_count`/`last_queried_at` not rebuildable on replay — already documented in ledger synthesis, not an operability blocker.
- **Load (100x):** drain is 1 batch of 100/call, no unbounded loop [VERIFIED creator.py:1490-1516]. 10,000 pending = 100 sequential embed batches interleaved with 25-signal `run_batch` calls; daemon hot-loops (`sleep 0`) while `claimed>0` [VERIFIED daemon.py:168], so a large backlog throttles ingestion to embedder speed with no backpressure/yield interval, and curator consolidate/reconcile load all groups/all indexed ids into memory (`fetch_all`, curator.py:274,399) — safe at current scale, OOM risk at 100x facts. Reasoned only, not load-tested.
- **Hostile input:** extraction caps unchanged (MAX_VALUE_CHARS=8192, MAX_CANDIDATES=200) [VERIFIED creator.py:98,102]; `_reject_suspicious`/`_unparseable_guard` still run before any new counting; drain `limit<=0` returns 0 [VERIFIED creator.py:1517]. Pre-existing unbounded `POST /memory/drain?limit=` (api/app.py:273, no `Query(ge/le)`) is unchanged by this branch — noted, not introduced.
- **Operability:** preview flag is in-memory only; restart re-previews (safe direction) [VERIFIED daemon.py:62-66]. Permanently-erroring phase = preview-forever with loud warning [VERIFIED daemon.py:246-261,311-319] — safe, but deletes never resume until fixed; operator must watch `daemon.curation_preview_incomplete` / `curator.dry_run_preview`. First tick is 6h after boot (`maintenance_interval` slept first, daemon.py:176), second tick deletes — there is a real 6h window to inspect preview counts, but nothing in operator docs tells the operator to look.
- **Reversal:** which test fails if the fix is reverted? Not run (read-only). Reasoned: reverting C02 drain calls fails tests/memory drain tests + `tests/daemon/test_drain_e2e.py`; reverting C04 daemon tick fails `tests/daemon/test_maintenance_loop.py` preview/fallback tests; reverting C03 touch fails touch-on-recall tests in context/api/cli/coder suites. Stated as reasoned-only.

## 4. Findings
### F-01 [MAJOR] Operator docs stale; first-production curator run + snapshot/rollback guidance missing
- Location: `docs/modules/daemon.md:29,60`, `docs/operations/deployment-and-runtime.md:57` vs `src/paa/daemon.py:170-182,200-321`; R3 mitigation in plan §L vs 0 doc hits for preview/snapshot.
- Evidence: docs still describe maintenance as bare `DecaySweeper.sweep()`; code now runs decay+prune-edges+consolidate+orphans+reconcile+analyze, with daemon `archive_writer=None` (daemon.py:227,273) so prune/consolidate/orphan deletions have no cold-lake summary and code-revert cannot restore them. Preview exists only in code docstring.
- Why it matters: R3 is the highest-impact open risk (Low×High). The mitigation as shipped is invisible to an operator reading the deployment/module docs; "revert the commit" (plan §K) will not bring deleted facts back.
- Recommended fix (root-cause): update the 3 doc lines to describe the curator + first-tick preview + 6h cadence, add a short operator note: snapshot cold lake before first deleting tick, inspect `daemon.curation_completed(dry_run=True)` counts, restore = cold-lake replay (not code revert). Docs-only; no code change needed.

### F-02 [MINOR] New error lines log unredacted exception text
- Location: `src/paa/memory/creator.py:1543` (`memory.embedding_drain_failed`, `error=str(exc)`), `src/paa/runtime.py:481` (`memory.embedding_sink` degraded, `error=str(exc)`), `src/paa/cli/main.py:647` (`console.print(f"embedding drain failed: {exc}")`).
- Evidence: house convention is `error=redact(str(exc))` (used by all 5 touch paths, daemon drain/curator lines, api drain) [VERIFIED grep]; `redact()` in `models/base.py:256-275` scrubs provider-credential patterns. `creator.py:1053,1485` unredacted lines are pre-existing; the 3 above are new in this diff [VERIFIED git diff]. Signal payloads flow through creator errors, so exception text can carry user content.
- Why it matters: inconsistent redaction is exactly how a secret/PII leaks into logs; CLI print also lands in terminal scrollback/CI logs.
- Recommended fix: wrap the 3 in `redact()` (cli/main.py already imports redact in `_touch_recalled`; import at module scope or reuse). Do not widen to pre-existing lines in this review.

### F-03 [MINOR] Concurrent drains duplicate embed work; curator scale is unbounded per phase
- Location: drain callers `daemon.py:165`, `api/app.py:282`, `cli/main.py:647` — no lock/shared guard between them; `curator.py:274,399` unbounded `fetch_all`.
- Evidence: daemon ingestion loop, API `POST /memory/drain`, and CLI `paa memory drain` can all call `drain_pending_embeddings()` on overlapping pending sets; `pending_embeddings` is fetch-then-mark with no claim, so two drains fetch the same 100 rows and both embed+upsert. Upsert is by fact id and `mark_embedded` sets the same status, so this is wasted embedder/vector cost (2x) and log noise, not corruption [VERIFIED facts.py:1057-1087, creator.py:1073-1091]. Reconcile builds the full live-id set in Python; consolidate loads all duplicate groups — memory grows with fact count, and budget is only checked between phases.
- Why it matters: at 100x backlog an API/CLI drain racing the daemon doubles real-embedder (torch) cost; a very large fact table can make one curator phase exceed memory/budget before the next budget check.
- Recommended fix (DO-LATER, not blocking): single-flight guard (asyncio.Lock) around `drain_pending_embeddings` for in-process callers; document that CLI is a separate process. Paginate reconcile/consolidate or stream ids. Neither is a correctness hole today.

### F-04 [MINOR] Backlog drain throughput / COUNT cadence is honest but unthrottled under load
- Location: `creator.py:1519-1563`, `daemon.py:150-168`.
- Evidence: one 100-batch per call; daemon sleeps 0 while signals claimed, so drain+ingest run back-to-back with no interval — progress is iterative and bounded, COUNT is 1 per non-idle call over the partial pending index (not a full-table scan) and skipped when idle [VERIFIED above]. Touch adds one `execute_many` transaction per recall batch [VERIFIED decay.py:305-316], bounded by delivered packet size.
- Why it matters: a 10k backlog monopolises the ingestion loop for ~100 embed batches; operators have `pending_count`/`backlog_remaining` on every drain line [VERIFIED creator.py:1556-1562] to observe it, but no rate/backpressure dial. Acceptable for first rollout; call it out in the F-01 doc note.
- Recommended fix: none required now; if embedder is real torch, consider draining N batches per ingestion tick or a small sleep when backlog is large. DO-LATER.

### F-05 [NIT] Plan §K rollback wording overstates reversibility
- Location: plan §K ("point of no return: none") vs F-01 evidence.
- Evidence: C01/C02 (sink/drain) and C05/C06 (counters/config) are genuinely revert-clean (pending facts stay pending; deprecation keeps old `.env` parsing [VERIFIED config.py:280-330]). C03 touch state and C04 curator deletions are not undone by revert.
- Recommended fix: one sentence in the F-01 doc note per change class. No code action.

## 5. Attempts to break it (reasoned, not executed against shared env)
- 100x backlog (10,000 pending, sink present): 100 drain calls × (fetch 100 + embed 100 + mark + COUNT). No unbounded loop; completes iteratively, `pending_count` decreases monotonically unless new facts arrive faster than 100/tick. NOT RUN (needs shared env / load harness).
- Concurrent drains (daemon + API POST during backlog): both fetch same head rows; both embed; both `mark_embedded(indexed)` — idempotent status, duplicate upsert same ids. Cost 2x, no lost facts, no `failed` flip on error [VERIFIED creator.py:1534-1549]. NOT RUN.
- Sink raises mid-drain: batch stays `pending`, error logged with gauge, return 0 [VERIFIED creator.py:1534-1549]; next tick retries head. No terminal failure. Covered by existing T02-pattern tests [RAN creator drain subset, 18 passed].
- First curator tick with empty entity index + provenanced facts: orphan phase skips with warning, deletes nothing [VERIFIED curator.py:349-368]; preview tick deletes nothing by construction (all delete phases count-only under `dry_run`) [VERIFIED curator.py:229-424]. Covered by [RAN tests/daemon/test_maintenance_loop.py + test_drain_e2e.py, 15 passed].
- Hostile payload through new counters: counters increment only, never change extraction outcome except first-person mapping; caps/guards unchanged. `_reject_suspicious` still raises before counting [VERIFIED creator.py:521-525].

## 6. Dimension checklist
- Correctness: not this review's lane (FINAL-01); no operability blocker found beyond F-01/F-02.
- Root cause vs symptom: F-01 fix is docs-truth, not a code band-aid.
- Regression/compat: no schema/config break — `decay_lambda` deprecated-not-removed, `prune_confidence_floor` explicit-only, both warn-and-ignore [VERIFIED config.py:278-330]; old `.env` keeps parsing. Rollback honesty: see F-01/F-05.
- Concurrency/idempotency: drain/touch idempotent, no lock (F-03); touch increments are single-statement `UPDATE … use_count+1` [VERIFIED decay.py:299-316].
- Error handling: drain/touch/curator failures isolated and logged; budget-stopped and phase-error paths resume or stay in preview safely [VERIFIED daemon.py:246-321, curator.py:206-225].
- Security/abuse: redaction mostly consistent (F-02); no new public API; ranking stays deterministic (D5 untouched). No LIKE patterns added outside `like.py` [VERIFIED diff: no storage/ files].
- Performance/scalability: F-03/F-04 — bounded per call, unbounded per curator phase at very large scale.
- Migration/rollback: no migration (verified, §2 row 1). Rollback docs: F-01/F-05.
- Observability: strong — `memory.embedding_sink` wired/degraded + embedder name [VERIFIED runtime.py:490-492], drain gauge with exact `pending_count` [VERIFIED creator.py:1556-1562], curator started/completed/preview-incomplete with full summary [VERIFIED daemon.py:292-320], drop counters on signal/batch lines [VERIFIED creator.py:1171-1210]. One gap: none of these are named in operator docs (F-01).
- Maintainability/conventions: `_touch_recalled` duplicated 4× (context/api/cli/coder) with identical semantics — pre-existing pattern of this branch, not introduced as a new abstraction problem; flag as DO-LATER dedup, not a finding against operability.
- Docs impact: F-01 is the docs impact.

## 7. Optimization opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| O1 | drain single-flight lock | F-03 | avoids 2x embed cost under concurrent drain | small | low | DO-LATER |
| O2 | paginate reconcile/consolidate | curator.py:274,399 | bounds memory at 100x facts | medium | low | DO-LATER |
| O3 | dedup `_touch_recalled` ×4 | 4 identical helpers | maintenance only | small | low | NOT-WORTH-IT in final pass (consistency is currently perfect; a shared helper is a refactor, not an operability fix) |

## 8. Cross-agent interactions
Waves sequenced creator.py (W1, W3) and daemon.py (W1, W2) single-writer per ledger; final diff shows no interleaving damage. `facts.py` untouched as planned (pending COUNT helper lives in creator.py:1565-1571) — compatible with the parity/ownership constraints.

## 9. What I could not verify
- 100x backlog, concurrent drains, and large-fact-table curator memory: reasoned from code only; no load test run (standing orders: scoped checks only, no shared-env load). [UNKNOWN load numbers]
- First-production-curator-run on real data: preview counts, actual eviction volumes, hash-embedder precision (R1 10-query spot-check) — operator steps, not done in this review. [UNKNOWN]
- Windows host gate vs Linux CI gate: only scoped tests + ruff on 5 files ran here; full gate is FINAL-01/orchestrator scope. [RAN: pytest tests/daemon/test_maintenance_loop.py tests/daemon/test_drain_e2e.py -> 15 passed; pytest tests/memory/test_e2e_remember_recall.py -> 18 passed; pytest tests/memory/test_creator.py -k "drain or pending or embed" -> 18 passed; ruff check src/paa/{daemon,memory/creator,memory/curator,runtime,cli/main}.py -> All checks passed]
- R2 (touch immortality): floors still apply and only delivered facts are touched [VERIFIED context_agents.py:323-328], so mitigation is honest; long-term eviction-starvation tuning remains open by design. [ASSUMED acceptable, operator-owned]

## Deploy / operator notes
1. Before first deleting curator tick: snapshot cold lake; after boot, read `daemon.curation_completed` with `dry_run=True` (first tick, ~6h after start) before the second tick deletes. Restarting the daemon re-previews — do not mistake repeated previews for a stuck system; check `daemon.curation_preview_incomplete`.
2. Docker image excludes `embeddings` extra by design (Dockerfile:27-30): production semantic recall runs the hash embedder until a derived image with `paa[embeddings]` is built. Log line `memory.embedding_sink status=wired embedder=…` names the actual embedder; run the plan's 10-paraphrase precision spot-check (R1) before trusting semantic ranking.
3. Watch `memory.embedding_drain` `pending_count` after deploy; it is exact and logged on success, sink-absent, and failure paths.
4. To roll back code: revert per-change commits (no migration). To restore curator-deleted facts: cold-lake replay, not code revert (F-01/F-05).
